# GitHub Project permissions for Clams

Clams reads and writes your GitHub Project board. That requires a token with
the **`project`** scope. This guide covers exactly what to do, how to verify
it, and the failure modes you may hit.

---

## 1. Which scope, and why

GitHub exposes two project scopes:

| Scope | Grants | Enough for Clams? |
|-------|--------|-------------------|
| `read:project` | Read queries only | **No** |
| `project` | Read **and** write (mutations) | **Yes** |

Clams moves cards between columns, which is a mutation
(`addProjectV2ItemById`, `updateProjectV2ItemFieldValue`). A read-only token
lets you render the board and then fail on the first write — worse than
having nothing, because it looks like it worked.

> **Note:** there is no `update:project` scope. If you see that name anywhere,
> it is wrong. The write scope is literally `project`.

Source: [GitHub — Using the API to manage Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)

---

## 2. Recommended: expand the existing `gh` credential

One command, no token recreation:

```bash
gh auth refresh -s project
```

This opens a browser and asks you to authorise the expanded permissions.
Approve it.

### Verify

```bash
gh auth status
```

The scope line must include `project`:

```
- Token scopes: 'gist', 'project', 'read:org', 'repo', 'workflow'
```

Then confirm the board is actually readable — this is the query that fails
without the scope:

```bash
gh api graphql -f query='{ viewer { login projectsV2(first:10){ nodes { number title } } } }'
```

You should see your projects listed. If you see `INSUFFICIENT_SCOPES`, the
scope did not land.

---

## 3. Alternative: create the token in the browser

If `gh auth refresh` does not offer the scope:

1. Go to **https://github.com/settings/tokens/new**
2. **Note:** `clams`
3. **Expiration:** your preference
4. **Select scopes** — tick:
   - `repo` (full control of private repositories)
   - `read:org`
   - `workflow`
   - `gist`
   - **`project`** ← *Read and write under the "Projects" heading*
5. Click **Generate token** and copy it immediately (shown once).

---

## 4. Update Clams' own configuration

**This is the step that gets missed.** Your `gh` login and Clams' token are
two separate credentials. Refreshing `gh` does **not** update Clams.

Clams reads its token from three environment files:

| File | Used by |
|------|---------|
| `.env.planner` | planner / orchestrator |
| `.env.executor` | executor |
| `.env.watchdog` | watchdog |

In **each** file, replace the value of:

```
CLAMS__GITHUB__TOKEN=<your new token>
```

Then restart the stack so the new value is picked up:

```bash
clamsctl recreate --yes
```

### Verify Clams can see the board

```bash
clams doctor
```

`github_reachability` should pass. Then confirm the project specifically:

```bash
gh api graphql -f query='{ repository(owner:"massimorofi", name:"clams") {
  projectV2(number: 3) { id title } } }'
```

---

## 5. Finding your project number

The number is in the board URL:

```
https://github.com/users/massimorofi/projects/3
                                            ^ user project, number 3
```

- **User (personal) project:** `https://github.com/users/<user>/projects/<n>`
  → query with `user(login:"<user>") { projectV2(number:<n>) { ... } }`
- **Organisation project:** `https://github.com/orgs/<org>/projects/<n>`
  → query with `organization(login:"<org>") { projectV2(number:<n>) { ... } }`

These are different GraphQL paths. Using the wrong one returns `NOT_FOUND`
even with a correctly scoped token.

---

## 6. Common failure modes

### `INSUFFICIENT_SCOPES ... requires ['read:project']`
The token lacks the scope. Do section 2 or 3.

### Works in your terminal, fails for Clams
Almost always the `.env` files. They hold a **separate, older** token. Do
section 4. Check with:

```bash
TOK=$(grep '^CLAMS__GITHUB__TOKEN=' .env.planner | cut -d= -f2)
curl -s -I -H "Authorization: Bearer $TOK" https://api.github.com/user \
  | grep -i oauth-scopes
```

That prints the scopes GitHub actually granted to **that** token.

### `gh auth token` → "no oauth token found for github.com"
`gh` stores its token in the system keyring, which needs a D-Bus session.
Non-interactive shells (cron, systemd services, CI, agent sandboxes) often
have no `DBUS_SESSION_BUS_ADDRESS`, so the keyring is unreachable even though
`gh auth status` in a real terminal works fine.

For anything running outside a desktop session, **use the `.env` token
explicitly** rather than relying on `gh`'s keyring.

### `Projects (classic) is being deprecated`
You are hitting the V1 API. Clams targets the **V2** schema
(`ProjectV2`, `ProjectV2ItemFieldSingleSelectOption`). Make sure you query
`projectV2` / `projectsV2`, not `project` / `projects`.

---

## 7. Security notes

- `.env.*` files are listed in `.gitignore` — **never commit them**. They
  contain a live credential.
- The `project` scope is powerful: it can rewrite any board your account can
  reach. Scope the token to the minimum lifetime you are comfortable with and
  rotate it periodically.
- If a token leaks, revoke it at https://github.com/settings/tokens and
  update all three `.env` files.
