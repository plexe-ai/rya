import { useState } from 'react'
import { KeyRound, LogIn, UserPlus, Users } from 'lucide-react'
import { API, getEmail, getSession, sessionPost } from '../lib/api'
import { useLoad } from '../lib/usePoll'
import type { ConsoleState } from '../lib/types'
import { Ago, CopyId, Empty, Mono, SecRow, Table, ViewHeader } from '../components/ui'

/**
 * Team & access — the one view in this console that needs the OTHER credential.
 *
 * Everything else here authenticates the *workspace* with an API key (`rya_sk_…`,
 * localStorage `rya_token`) and goes through `api()`. Members, keys and passwords
 * authenticate the *user*: they are `/v1/*` account routes that take the session
 * token minted by `POST /v1/login` / `/v1/signup` (localStorage `rya_session`).
 *
 * Three consequences drive the whole layout, and none of them is an error state:
 *
 *  1. **A workspace key without a session is normal.** It is exactly what someone
 *     who pasted a key into the auth modal has. Team management then simply cannot
 *     be answered — so the view says "sign in", rather than reporting an outage or
 *     rendering an empty members table as though the workspace had nobody in it.
 *  2. **Listing keys, inviting, revoking and removing are owner-only** server-side
 *     (`_require_access(..., need_owner=True)`), so a member gets a 403 on those and
 *     a 403 here means "you are not the owner", not "something broke".
 *  3. **These routes are multi-tenant only** (`_require_mt()`). In single-tenant mode
 *     there are no accounts, workspaces or invites at all, so the view explains that
 *     instead of firing requests that would come back 400.
 *
 * The `/v1/*` routes are workspace/session-scoped, not agent-scoped: no `ag()` prefix.
 */

// ---- response shapes (local on purpose: only this view reads them) -----------

/** `GET /v1/workspaces/{ws}/members` — `tenancy.list_members`. */
interface Member {
  email: string
  role: string
  /** False while the invite is outstanding: the email has no account yet. */
  claimed: boolean
  invitedAt?: string
}

/**
 * `GET /v1/workspaces/{ws}/keys` — METADATA ONLY. The store keeps a SHA-256 hash
 * and never the key, so there is deliberately no field here that could be rendered
 * as a secret, and nothing in this table implies the value is retrievable.
 */
interface KeyMeta {
  id: string
  label?: string | null
  createdAt?: string
  createdBy?: string | null
}

type Role = 'owner' | 'member' | 'outsider'

interface Team {
  role: Role
  /** Null when the signed-in account is not a member of this workspace at all. */
  members: Member[] | null
  /** Null for a member: the key list is owner-only. */
  keys: KeyMeta[] | null
}

// ---- session-authenticated GET / DELETE --------------------------------------
// lib/api.ts exports `sessionPost` but no session GET or DELETE, and this is the
// only view that needs them, so they live here rather than growing the shared
// module. Same header, same `{detail:{message}}` unwrapping — plus the status,
// because 403 has to be told from a genuine failure.

const SESSION_KEY = 'rya_session'

class SessionError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'SessionError'
    this.status = status
  }
}

const statusOf = (e: unknown) => (e instanceof SessionError ? e.status : 0)

async function sessionFetch<T>(path: string, method: 'GET' | 'DELETE'): Promise<T> {
  const session = getSession()
  if (!session) throw new SessionError('Not signed in.', 0)
  const r = await fetch(API + path, {
    method,
    headers: { 'content-type': 'application/json', Authorization: `Bearer ${session}` },
  })
  const d = await r.json().catch(() => ({}))
  // A rejected session is a stale session: drop it so the rest of the console stops
  // claiming to be signed in. The workspace API key is a different credential and is
  // deliberately left alone — losing it would sign the operator out of everything.
  if (r.status === 401) localStorage.removeItem(SESSION_KEY)
  if (!r.ok) throw new SessionError(d?.detail?.message || `HTTP ${r.status}`, r.status)
  return d as T
}

const enc = encodeURIComponent

export function TeamView({
  state,
  onToast,
  onSignIn,
}: {
  state: ConsoleState
  onToast: (m: string) => void
  /**
   * Raise the auth modal. Optional because only the no-session card needs it — with
   * a session, nothing on this page asks you to sign in. Without it that card can
   * only *describe* where the button is, which is a worse version of the same thing.
   */
  onSignIn?: () => void
}) {
  const multiTenant = state.runtime.multiTenant
  // `viewer.workspace` is the DISPLAY NAME (the id only when they coincide, i.e. a
  // workspace whose row has no name); `viewer.workspaceId` is what these routes key
  // on. The legacy console passes `viewer.workspace`, which 403s for any workspace
  // that has been named — see the note at the bottom of this file.
  const wsId = state.viewer?.workspaceId || state.viewer?.workspace || ''
  const email = getEmail()

  // Session presence is React state, not a bare read: a 401 mid-session has to flip
  // the page to the sign-in card, and that is a render, not a redirect.
  const [signedIn, setSignedIn] = useState(() => !!getSession())
  const [invite, setInvite] = useState('')
  const [pwCurrent, setPwCurrent] = useState('')
  const [pwNext, setPwNext] = useState('')
  const [busy, setBusy] = useState(false)
  /**
   * A minted key, held only for as long as it is on screen. It is shown once and
   * never stored here — the server keeps a hash, so this render is the only chance
   * anyone has to copy it.
   */
  const [minted, setMinted] = useState<{ label: string; key: string } | null>(null)

  // On entry, not on the shell's 6s poll: membership changes when someone invites or
  // removes a teammate, not per second.
  const { data, error, loading, reload } = useLoad<Team | null>(async () => {
    // Nothing to ask for, and asking would be worse than not asking: single-tenant
    // 400s on every one of these routes, and with no session they would all 401.
    if (!multiTenant || !signedIn || !wsId) return null

    let members: Member[]
    try {
      members = (await sessionFetch<{ members?: Member[] }>(`/v1/workspaces/${enc(wsId)}/members`, 'GET')).members ?? []
    } catch (e) {
      if (statusOf(e) === 403) return { role: 'outsider', members: null, keys: null }
      if (statusOf(e) === 401) {
        setSignedIn(false)
        return null
      }
      throw e
    }

    // Owner-only. A 403 here is the answer "you are a member", so the members table
    // still renders and only the key section is withheld.
    let keys: KeyMeta[] | null = null
    let role: Role = 'owner'
    try {
      keys = (await sessionFetch<{ keys?: KeyMeta[] }>(`/v1/workspaces/${enc(wsId)}/keys`, 'GET')).keys ?? []
    } catch (e) {
      if (statusOf(e) === 403) role = 'member'
      else if (statusOf(e) === 401) {
        setSignedIn(false)
        return null
      } else throw e
    }
    return { role, members, keys }
  }, [multiTenant, signedIn, wsId])

  /** One error path for every write: toast the server's prose, never a status code. */
  async function run(label: string, fn: () => Promise<string>) {
    setBusy(true)
    try {
      onToast(await fn())
    } catch (e) {
      onToast(`${label} failed — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const doInvite = () => {
    const to = invite.trim()
    if (!to) return
    return run('Invite', async () => {
      const d = await sessionPost<{ claimed?: boolean }>(`/v1/workspaces/${enc(wsId)}/members`, {
        email: to,
      })
      setInvite('')
      await reload()
      // Two real outcomes: an existing account gets access now, an unknown email gets
      // an invite that is claimed at signup. Saying which one avoids a support ticket.
      return `Invited ${to}${d.claimed ? ' — access is live now' : ' — access starts at signup'}`
    })
  }

  const doRemove = (m: Member) => {
    // Not recoverable, so it is stated before the click as well as in the header row.
    if (
      !window.confirm(
        `Remove ${m.email} and revoke every API key they minted for this workspace? Anything using those keys stops working immediately.`,
      )
    )
      return
    return run('Remove', async () => {
      const d = await sessionFetch<{ keysRevoked?: number }>(
        `/v1/workspaces/${enc(wsId)}/members/${enc(m.email)}`,
        'DELETE',
      )
      await reload()
      return `Removed ${m.email} · ${d.keysRevoked ?? 0} key(s) revoked`
    })
  }

  const doRevoke = (k: KeyMeta) => {
    if (!window.confirm(`Revoke key ${k.id}? Anything using it stops working immediately.`)) return
    return run('Revoke', async () => {
      await sessionFetch(`/v1/workspaces/${enc(wsId)}/keys/${enc(k.id)}`, 'DELETE')
      await reload()
      return 'Key revoked'
    })
  }

  const doMint = () =>
    run('Mint', async () => {
      const d = await sessionPost<{ apiKey: string; workspace?: { name?: string } }>(
        `/v1/workspaces/${enc(wsId)}/keys`,
        {},
      )
      setMinted({ label: d.workspace?.name || wsId, key: d.apiKey })
      await reload()
      return 'Key minted — copy it now, it is shown once'
    })

  const doPassword = () => {
    if (!pwCurrent || pwNext.length < 8) {
      onToast('Enter your current password and a new one (8+).')
      return
    }
    return run('Update', async () => {
      await sessionPost('/v1/password', { current: pwCurrent, new: pwNext })
      setPwCurrent('')
      setPwNext('')
      return 'Password updated'
    })
  }

  const header = (
    <ViewHeader title="Team &amp; access">
      Members, invites and workspace API keys. These are <strong>account</strong> operations: they
      use the session from your email sign-in, not the workspace API key the rest of the console
      runs on.
    </ViewHeader>
  )

  // ---- the states that are answers, not failures ----------------------------

  if (!multiTenant) {
    return (
      <>
        {header}
        <div className="downcard">
          <div className="dic">
            <Users aria-hidden="true" focusable="false" />
          </div>
          <h3>Single-tenant runtime</h3>
          <p>
            There are no accounts, workspaces or invites here — access is a single operator token,
            which you set with the <span className="mono">Workspace</span> button in the sidebar. Run
            with <span className="mono">RYA_MULTI_TENANT=1</span> and a database to get teams.
          </p>
        </div>
      </>
    )
  }

  if (!signedIn) {
    return (
      <>
        {header}
        <div className="downcard">
          <div className="dic">
            <LogIn aria-hidden="true" focusable="false" />
          </div>
          <h3>Sign in to manage the team</h3>
          <p>
            You are connected with a workspace API key, which scopes data access but says nothing
            about who you are. Members, keys and passwords need your account session — sign in with
            your email. Nothing is wrong with this workspace; it just cannot answer who is in it
            until it knows who is asking.
          </p>
          {onSignIn && (
            <div className="dbtns">
              <button className="btn dark sm" onClick={onSignIn}>
                <LogIn aria-hidden="true" focusable="false" />
                Sign in
              </button>
            </div>
          )}
        </div>
      </>
    )
  }

  if (!wsId) {
    return (
      <>
        {header}
        <Empty icon={Users}>
          This runtime did not report a workspace id, so there is no workspace to manage.
        </Empty>
      </>
    )
  }

  if (loading && !data) {
    return (
      <>
        {header}
        <Empty icon={Users}>Loading…</Empty>
      </>
    )
  }

  if (error || !data) {
    return (
      <>
        {header}
        <Empty icon={Users}>Team unavailable — {error || 'no response'}</Empty>
      </>
    )
  }

  const account = (
    <>
      <SecRow left="Your account" right={<Mono>{email || 'signed in'}</Mono>} />
      <div className="gcard">
        <div className="gch">
          <span className="gct">Change password</span>
        </div>
        <form
          className="grow2"
          style={{ marginTop: 0 }}
          onSubmit={(e) => {
            e.preventDefault()
            void doPassword()
          }}
        >
          <input
            className="gin"
            type="password"
            placeholder="current password"
            aria-label="Current password"
            value={pwCurrent}
            onChange={(e) => setPwCurrent(e.target.value)}
          />
          <input
            className="gin"
            type="password"
            placeholder="new password (8+)"
            aria-label="New password"
            value={pwNext}
            onChange={(e) => setPwNext(e.target.value)}
          />
          <button className="btn dark sm" type="submit" disabled={busy}>
            <KeyRound aria-hidden="true" focusable="false" />
            Update
          </button>
        </form>
      </div>
    </>
  )

  // Signed in, but this account is on no membership row for the workspace whose key
  // the browser holds. Two credentials for two different things, pointed at two
  // different tenants — worth saying plainly instead of showing an empty table.
  if (data.role === 'outsider') {
    return (
      <>
        {header}
        <div className="gcard">
          <div className="gch">
            <span className="gct">No access to this workspace</span>
          </div>
          <div className="sub" style={{ margin: 0 }}>
            You are signed in as <Mono>{email || 'this account'}</Mono>, and that account is not a
            member of the workspace this API key belongs to. Ask its owner for an invite, or open a
            workspace you do belong to from the <span className="mono">Workspace</span> button.
          </div>
        </div>
        {account}
      </>
    )
  }

  const owner = data.role === 'owner'

  return (
    <>
      {header}

      {owner ? (
        <>
          <SecRow
            left="Members"
            right={`${data.members?.length ?? 0} invited · removing a member revokes every key they minted`}
          />
          <Table
            rows={data.members ?? []}
            rowKey={(m) => m.email}
            emptyMessage="No members yet — invite your team below."
            emptyIcon={Users}
            columns={[
              { header: 'Email', cell: (m) => <Mono>{m.email}</Mono> },
              { header: 'Role', cell: (m) => <span className="ptag">{m.role}</span> },
              {
                // `StatusBadge` maps RUN statuses; membership has its own two words,
                // so the modifier is picked here rather than teaching format.ts about
                // them (same shape ChannelsView uses for enabled/disabled).
                header: 'Status',
                cell: (m) => (
                  <span className={`stbadge ${m.claimed ? 'ok' : 'wait'}`}>
                    <span className="d" />
                    {m.claimed ? 'active' : 'invited'}
                  </span>
                ),
              },
              {
                header: 'Invited',
                cell: (m) => (
                  <span className="dim">
                    <Ago ts={m.invitedAt} />
                  </span>
                ),
              },
              {
                header: '',
                cell: (m) => (
                  <button
                    className="btn sm"
                    disabled={busy}
                    title="Remove this member and revoke every key they minted"
                    onClick={() => void doRemove(m)}
                  >
                    Remove
                  </button>
                ),
              },
            ]}
          />

          <form
            className="grow2"
            style={{ margin: '10px 0 0' }}
            onSubmit={(e) => {
              e.preventDefault()
              void doInvite()
            }}
          >
            {/* Controlled input: the value lives in React state, so no refresh can
                clobber the caret the way the legacy console's re-render did. */}
            <input
              className="gin"
              style={{ flex: 1, minWidth: 220 }}
              placeholder="teammate@company.com"
              aria-label="Invite a teammate by email"
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
            />
            <button className="btn dark sm" type="submit" disabled={busy || !invite.trim()}>
              <UserPlus aria-hidden="true" focusable="false" />
              Invite
            </button>
          </form>

          <SecRow
            left="API keys"
            right="metadata only — a key value is shown once, at mint time, and never stored"
          />

          {minted && (
            <div className="keynote">
              New API key for <span className="mono">{minted.label}</span>. This is the only time it
              will ever be shown — only its SHA-256 hash is stored, so there is no way to look it up
              later. Copy it somewhere safe; it is not saved in this browser.
              <br />
              <span className="mono">{minted.key}</span>
              <br />
              <button className="btn sm" style={{ marginTop: 8 }} onClick={() => setMinted(null)}>
                Done, I copied it
              </button>
            </div>
          )}

          <Table
            rows={data.keys ?? []}
            rowKey={(k) => k.id}
            emptyMessage="No keys."
            emptyIcon={KeyRound}
            columns={[
              { header: 'Label', cell: (k) => k.label || '—' },
              { header: 'Created', cell: (k) => <span className="dim"><Ago ts={k.createdAt} /></span> },
              {
                header: 'Key id',
                cell: (k) => (
                  <span className="dim">
                    <CopyId id={k.id} onCopied={onToast} />
                  </span>
                ),
              },
              {
                header: '',
                cell: (k) => (
                  <button className="btn sm" disabled={busy} onClick={() => void doRevoke(k)}>
                    Revoke
                  </button>
                ),
              },
            ]}
          />

          <div className="grow2">
            <button className="btn sm" disabled={busy} onClick={() => void doMint()}>
              <KeyRound aria-hidden="true" focusable="false" />
              Mint a key
            </button>
            <span className="dim">for this workspace, attributed to your account</span>
          </div>
        </>
      ) : (
        <>
          {/* A member sees the roster (that route only needs membership) and is told
              plainly why the key section is absent. A 403 is an answer here. */}
          <div className="gcard">
            <div className="gch">
              <span className="gct">Membership</span>
              <span className="ptag">member</span>
            </div>
            <div className="sub" style={{ margin: 0 }}>
              You are a <strong>member</strong> of this workspace, not the owner of this workspace.
              Invites, API keys and removals are the owner&apos;s to manage.
            </div>
          </div>

          <SecRow left="Members" right={`${data.members?.length ?? 0} in this workspace`} />
          <Table
            rows={data.members ?? []}
            rowKey={(m) => m.email}
            emptyMessage="No members listed."
            emptyIcon={Users}
            columns={[
              { header: 'Email', cell: (m) => <Mono>{m.email}</Mono> },
              { header: 'Role', cell: (m) => <span className="ptag">{m.role}</span> },
              {
                header: 'Status',
                cell: (m) => (
                  <span className={`stbadge ${m.claimed ? 'ok' : 'wait'}`}>
                    <span className="d" />
                    {m.claimed ? 'active' : 'invited'}
                  </span>
                ),
              },
              {
                header: 'Invited',
                cell: (m) => (
                  <span className="dim">
                    <Ago ts={m.invitedAt} />
                  </span>
                ),
              },
            ]}
          />

          <div className="grow2">
            <span className="dim">
              Need a key for this workspace? Mint one from the <span className="mono">Workspace</span>{' '}
              button — that route is open to members.
            </span>
          </div>
        </>
      )}

      {account}
    </>
  )
}
