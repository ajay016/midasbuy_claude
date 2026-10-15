from django.test import TestCase

from apiauth.models import User

from . import services as cs
from .models import CodeEvent, CodeStatus, StoredCode


def _user(email, role=User.ROLE_CLIENT, partner=None):
    return User.objects.create_user(email=email, password="x", name=email.split("@")[0],
                                    role=role, partner=partner, can_order=True)


class CodeVaultTests(TestCase):
    def setUp(self):
        self.admin = _user("admin@x.io", User.ROLE_ADMIN)
        self.partner = _user("p@x.io", User.ROLE_PARTNER)
        self.client_a = _user("a@x.io", partner=self.partner)
        self.other = _user("o@x.io")

    def test_add_normalizes_dedupes_and_encrypts(self):
        out = cs.add_codes(self.client_a, ["AbCd 1234", "AbCd1234", "", "ZZZZ9999"])
        self.assertEqual(len(out["added"]), 2)
        self.assertEqual(out["duplicates"], ["••••1234"])
        self.assertEqual(out["empty"], 1)
        sc = StoredCode.objects.get(owner=self.client_a, code_last4="1234")
        self.assertNotIn("AbCd1234", sc.code_encrypted)        # stored encrypted
        self.assertEqual(sc.get_code(), "AbCd1234")             # case kept
        # Same code again later -> duplicate, not a second row.
        again = cs.add_codes(self.client_a, ["AbCd1234"])
        self.assertEqual((len(again["added"]), again["duplicates"]), (0, ["••••1234"]))

    def test_scope_client_partner_admin_other(self):
        cs.add_codes(self.client_a, ["CLIENTCODE1"])
        cs.add_codes(self.partner, ["PARTNERCODE"])
        cs.add_codes(self.other, ["OTHERCODE01"])
        seen = lambda u: {i["code_last4"] for i in cs.list_codes(u)["items"]}
        self.assertEqual(seen(self.client_a), {"ODE1"})
        self.assertEqual(seen(self.partner), {"ODE1", "CODE"})   # own + own client's
        self.assertEqual(seen(self.other), {"DE01"})
        self.assertEqual(seen(self.admin), {"ODE1", "CODE", "DE01"})
        sc = StoredCode.objects.get(owner=self.other)
        self.assertIsNone(cs.get_code(self.client_a, sc.pk))
        self.assertIsNotNone(cs.get_code(self.admin, sc.pk))

    def test_redemption_marks_used_and_redeemed_to_visibility(self):
        cs.add_codes(self.client_a, ["REDEEMME123"])
        cs.record_redemption("REDEEMME123", actor=self.client_a, source="api",
                             player_id="5421364032", username="ErrorSajal",
                             product_name="60 UC", order_no="ORD1")
        sc = StoredCode.objects.get(owner=self.client_a)
        self.assertEqual((sc.status, sc.redeemed_player_id), (CodeStatus.USED, "5421364032"))
        self.assertEqual(cs.redeemed_to_for(self.client_a, "REDEEMME123")["player_id"], "5421364032")
        self.assertIsNotNone(cs.redeemed_to_for(self.partner, "REDEEMME123"))  # client's partner
        self.assertIsNotNone(cs.redeemed_to_for(self.admin, "REDEEMME123"))
        self.assertIsNone(cs.redeemed_to_for(self.other, "REDEEMME123"))       # stranger

    def test_unstored_code_redeem_is_still_recorded(self):
        cs.record_redemption("NEVERSTORED1", actor=self.other, player_id="111", source="api")
        self.assertEqual(cs.redeemed_to_for(self.other, "NEVERSTORED1")["player_id"], "111")
        # Storing it afterwards starts it as used, with the player.
        cs.add_codes(self.other, ["NEVERSTORED1"])
        sc = StoredCode.objects.get(owner=self.other)
        self.assertEqual((sc.status, sc.redeemed_player_id), (CodeStatus.USED, "111"))

    def test_check_only_applies_definite_results(self):
        cs.add_codes(self.client_a, ["CHECKME1234"])
        cs.record_check("CHECKME1234", None, actor=self.client_a)        # e.g. timeout
        self.assertEqual(StoredCode.objects.get().status, CodeStatus.UNCHECKED)
        st = cs.definite_status("invalid", {"err_code": "REDEEM_CODE_EXPIRED"})
        cs.record_check("CHECKME1234", st, actor=self.client_a)
        self.assertEqual(StoredCode.objects.get().status, CodeStatus.EXPIRED)
        self.assertEqual(cs.definite_status("invalid", {"err_code": "UPSTREAM_TIMEOUT"}), None)

    def test_redeem_lock_blocks_parallel_and_used(self):
        cs.add_codes(self.client_a, ["LOCKME12345"])
        sc = StoredCode.objects.get()
        self.assertTrue(cs.begin_redeem(sc))
        self.assertFalse(cs.begin_redeem(sc))                 # second request
        cs.end_redeem(sc, CodeStatus.UNCHECKED)
        self.assertTrue(cs.begin_redeem(sc))
        StoredCode.objects.filter(pk=sc.pk).update(status=CodeStatus.USED)
        self.assertFalse(cs.begin_redeem(sc))                 # already used

    def test_history_hides_strangers_actions(self):
        cs.add_codes(self.client_a, ["SHARED00001"])
        cs.add_codes(self.other, ["SHARED00001"])
        cs.record_redemption("SHARED00001", actor=self.other, player_id="999", source="api")
        fp = cs.fingerprint("SHARED00001")
        kinds_a = [(e["kind"], e["player_id"]) for e in cs.history(self.client_a, fp)]
        self.assertNotIn(("redeemed", "999"), kinds_a)     # other user's redeem hidden
        self.assertIn(("redeemed", "999"), [(e["kind"], e["player_id"])
                                            for e in cs.history(self.other, fp)])
        # ...but client_a's stored copy still shows it as used (a fact about the code).
        self.assertEqual(StoredCode.objects.get(owner=self.client_a).status, CodeStatus.USED)

    def test_delete_keeps_history(self):
        cs.add_codes(self.client_a, ["DELETEME001"])
        sc = StoredCode.objects.get()
        cs.delete_code(self.client_a, sc)
        self.assertFalse(StoredCode.objects.exists())
        self.assertEqual(sorted(CodeEvent.objects.values_list("kind", flat=True)), ["added", "deleted"])
