# Code vault and redemption records

Status: implemented (2026-10-15).

## Goals

1. **"Used on which player?"** When a code checked through our app turns out to be
   used, show the player it was redeemed to — for codes **our app** redeemed.
   Midasbuy itself never reveals this (verified: the used-code response carries no
   player, and no Midasbuy endpoint maps a code to a player).
2. **Code vault.** Partners, clients and staff can store their codes on our server,
   then list them by status, check them, redeem them, see each code's history, and
   delete them — in the panel and through the API.
3. **Redeeming isn't tied to ownership.** Anyone allowed to order can redeem any
   code, stored or not, theirs or not. All ownership decisions go through one
   function (`codes.services.can_redeem`) so this can be tightened later in one
   place.

## Data model (new Django app `codes`)

`StoredCode` — one code in one owner's vault.

| Field | Notes |
|---|---|
| `owner` | User the code belongs to (the billing user: a client, or a partner acting for itself) |
| `code_encrypted` | Full code, Fernet-encrypted with `APIAUTH_FERNET_KEY` (needed to redeem later) |
| `fingerprint` | HMAC-SHA256 of the normalized code — lookups/dedupe without decrypting |
| `code_last4` | For display (`••••Z4`) |
| `label`, `country_code` | Optional owner note; storefront (default `bd`) |
| `status` | `unchecked` · `valid` · `used` · `expired` · `invalid` · `redeeming` |
| `product_name`, `checked_at` | From the last check / redeem |
| `redeemed_player_id`, `redeemed_username`, `redeemed_at`, `redeemed_by` | Filled when our app redeems it |
| unique | (`owner`, `fingerprint`) |

`CodeEvent` — append-only history, kept even if the stored code is deleted.

| Field | Notes |
|---|---|
| `fingerprint`, `code_last4` | Which code (works for codes that were never stored) |
| `stored_code` | FK, nullable, `SET_NULL` |
| `kind` | `added` · `checked` · `redeemed` · `redeem_failed` · `deleted` |
| `actor` | User who did it |
| `player_id`, `username`, `product_name`, `order_no`, `result_status`, `message`, `source` | Details (`source` = panel · api · bulk · vault) |

Normalization: whitespace removed, case kept (Midasbuy codes are case-sensitive).

## Who sees what

- **Vault rows:** owner; the owner's partner; admins.
- **`redeemed_to` on a used code:** admins; the user who redeemed it (and that
  user's partner); anyone who has that code in their vault (and their partner).
  Otherwise one partner could learn another partner's customers' player IDs.

## Recording (every path)

Every successful redeem through our app writes a `redeemed` event and updates any
`StoredCode` rows with that fingerprint: panel two-step `/api/redeem`,
`/api/redeem-now`, bulk redeem, and vault redeem. Every check that returns a
definite status (`valid`/`used`/`expired`/`invalid`) updates stored rows' status.

## API (`/api/codes`, same auth as the rest; signed calls bill `X-Client-Id`)

| Method & path | What | Cost |
|---|---|---|
| `POST /api/codes` | Store codes `{codes: [...], label?, country_code?}` → added / duplicates | free |
| `GET /api/codes` | List: `status` (`unused` = unchecked+valid), `search` (last 4 / label), `client_ref` (partners/admins), `page`, `page_size` | free |
| `GET /api/codes/{code_id}` | Detail incl. full code and history | free |
| `POST /api/codes/{code_id}/check` | Check status via Midasbuy `{player_id?, country_code?}` | 1 |
| `POST /api/codes/{code_id}/redeem` | Redeem to `{player_id, country_code?}` | 1 |
| `DELETE /api/codes/{code_id}` | Remove from vault (history kept) | free |
| `POST /api/codes/lookup` | Any code `{code}` → stored info, visible history, `redeemed_to` | free |

Checking needs a player ID (Midasbuy's check is per player). If omitted, the
server uses `MIDASBUY_CODE_CHECK_PLAYER_ID` from settings; if that's unset → 400.

Existing endpoints gain an optional `redeemed_to` field when a code is `used` and
the caller may see it: `/api/code-status`, `/api/redeem-now`, `/api/redeem`.

A stored code being redeemed is set to `redeeming` atomically, so two parallel
requests can't redeem the same stored code twice (the second gets 409).

## Panel

New page **My Codes** (Operations menu, for anyone allowed to order): add codes
(paste one per line), filter by status, search, per-row Check / Redeem / History /
Delete. Partners and admins see an Owner column and a client filter.

## Fixed along the way

- `GET /api/bulk/jobs/{id}` (and `/items`) had no ownership check: any user could
  read any job's pin codes by guessing ids. Jobs now record `requested_by`; only
  the owner, the owner's partner, or an admin can read them. Older jobs without an
  owner are admin-only.

## Not done (on purpose)

- Restricting redemption to the code's owner (see goal 3).
- Backfilling records from old bulk jobs (can be added as a one-off command).
