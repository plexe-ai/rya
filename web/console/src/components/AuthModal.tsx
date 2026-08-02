import { useEffect, useRef, useState } from 'react'
import { ArrowRight, GitCommitVertical, LogIn, Plug, Plus, Sparkles } from 'lucide-react'
import { API, authPost, saveSession, sessionPost, setToken } from '../lib/api'
import type { RuntimeInfo, Workspace } from '../lib/types'

type Tab = 'signup' | 'login' | 'key' | 'ws'

/**
 * Signup / login / API-key entry, ported from the legacy console's auth modal.
 *
 * The runtime has two shapes and the modal follows `GET /v1/info`:
 *  - single-tenant: there are no accounts, only an operator token, so only the
 *    "API key" tab is meaningful and the others are hidden.
 *  - multi-tenant: signup mints a workspace + key; login lists workspaces and
 *    minting a key for one is what actually opens the console.
 *
 * The session token (account-scoped) and the workspace API key are different
 * credentials stored under different keys — see lib/api.ts.
 */
export function AuthModal({ onClose, onAuthed }: { onClose: () => void; onAuthed: () => void }) {
  const [info, setInfo] = useState<RuntimeInfo | null>(null)
  const [tab, setTab] = useState<Tab>('signup')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null)
  const [issuedKey, setIssuedKey] = useState<{ name: string; key: string } | null>(null)

  const cardRef = useRef<HTMLDivElement>(null)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  const multiTenant = !!info?.multiTenant

  useEffect(() => {
    fetch(`${API}/v1/info`)
      .then((r) => r.json())
      .then((d: RuntimeInfo) => {
        setInfo(d)
        // No accounts in single-tenant mode: an operator token is the only way in.
        if (!d.multiTenant) setTab('key')
      })
      .catch(() => setInfo({}))
  }, [])

  useEffect(() => {
    firstFieldRef.current?.focus()
  }, [tab])

  // Escape closes; Tab is trapped inside the dialog.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab' || !cardRef.current) return
      const f = cardRef.current.querySelectorAll<HTMLElement>('input,button')
      if (!f.length) return
      const first = f[0]!
      const last = f[f.length - 1]!
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  function enterWithKey(key: string) {
    setToken(key)
    onAuthed()
  }

  async function guard(fn: () => Promise<void>) {
    setErr('')
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const submitSignup = (form: HTMLFormElement) =>
    guard(async () => {
      const fd = new FormData(form)
      const email = String(fd.get('email') || '').trim()
      const password = String(fd.get('password') || '')
      const workspaceName = String(fd.get('workspace') || '').trim() || 'My workspace'
      if (!email || password.length < 8) {
        throw new Error('Enter an email and an 8+ character password.')
      }
      const d = await authPost<{ token?: string; apiKey: string; workspace: { name: string } }>(
        '/v1/signup',
        { email, password, workspaceName },
      )
      if (d.token) saveSession(d.token, email)
      // Show the key exactly once — it is not retrievable later.
      setIssuedKey({ name: d.workspace.name, key: d.apiKey })
      setTab('ws')
    })

  const submitLogin = (form: HTMLFormElement) =>
    guard(async () => {
      const fd = new FormData(form)
      const email = String(fd.get('email') || '').trim()
      const password = String(fd.get('password') || '')
      if (!email || !password) throw new Error('Enter your email and password.')
      const d = await authPost<{ token: string; workspaces?: Workspace[] }>('/v1/login', {
        email,
        password,
      })
      saveSession(d.token, email)
      setWorkspaces(d.workspaces || [])
      setTab('ws')
    })

  const submitKey = (form: HTMLFormElement) =>
    guard(async () => {
      const v = String(new FormData(form).get('token') || '').trim()
      if (!v) return
      enterWithKey(v)
    })

  const openWorkspace = (ws: Workspace) =>
    guard(async () => {
      const d = await sessionPost<{ apiKey: string; workspace?: { name?: string } }>(
        `/v1/workspaces/${encodeURIComponent(ws.id)}/keys`,
        {},
      )
      setIssuedKey({ name: d.workspace?.name || ws.name, key: d.apiKey })
    })

  const createWorkspace = (form: HTMLFormElement) =>
    guard(async () => {
      const name = String(new FormData(form).get('name') || '').trim() || 'Workspace'
      const d = await sessionPost<{ apiKey: string; workspace: { name: string } }>(
        '/v1/workspaces',
        { name },
      )
      setIssuedKey({ name: d.workspace.name, key: d.apiKey })
    })

  const invite = (ws: Workspace) =>
    guard(async () => {
      const email = prompt(`Invite a teammate to "${ws.name}" - their email:`)
      if (!email) return
      const d = await sessionPost<{ ok?: boolean; claimed?: boolean }>(
        `/v1/workspaces/${encodeURIComponent(ws.id)}/members`,
        { email },
      )
      setErr(
        d.ok
          ? `Invited ${email}${d.claimed ? ' - access is live now.' : ' - access starts when they sign up.'}`
          : 'Invite failed',
      )
    })

  return (
    <div className="authwrap" style={{ display: 'grid' }}>
      <div className="authcard" role="dialog" aria-modal="true" aria-labelledby="authTitle" ref={cardRef}>
        <div className="al">
          <GitCommitVertical aria-hidden="true" focusable="false" />
        </div>
        <h3 id="authTitle">Welcome to Rya</h3>
        <p>
          {multiTenant
            ? 'Create an account to get a workspace + API key, or connect with a key you have.'
            : 'This runtime requires an operator token. It is stored only in your browser.'}
        </p>

        <div className="authtabs">
          {(['signup', 'login', 'key'] as const).map((t) =>
            t !== 'key' && !multiTenant ? null : (
              <button
                key={t}
                className={`atab${tab === t ? ' on' : ''}`}
                onClick={() => {
                  setTab(t)
                  setErr('')
                }}
              >
                {t === 'signup' ? 'Sign up' : t === 'login' ? 'Sign in' : 'API key'}
              </button>
            ),
          )}
        </div>

        {tab === 'signup' && (
          <form
            className="apanel"
            onSubmit={(e) => {
              e.preventDefault()
              void submitSignup(e.currentTarget)
            }}
          >
            <input ref={firstFieldRef} name="email" type="email" placeholder="you@company.com" aria-label="Email" />
            <input name="password" type="password" placeholder="Password (8+ characters)" aria-label="Password" />
            <input name="workspace" placeholder="Workspace name (e.g. Acme)" aria-label="Workspace name" />
            <button className="btn dark" type="submit" disabled={busy}>
              <Sparkles aria-hidden="true" focusable="false" />
              Create account
            </button>
          </form>
        )}

        {tab === 'login' && (
          <form
            className="apanel"
            onSubmit={(e) => {
              e.preventDefault()
              void submitLogin(e.currentTarget)
            }}
          >
            <input ref={firstFieldRef} name="email" type="email" placeholder="you@company.com" aria-label="Email" />
            <input name="password" type="password" placeholder="Password" aria-label="Password" />
            <button className="btn dark" type="submit" disabled={busy}>
              <LogIn aria-hidden="true" focusable="false" />
              Sign in
            </button>
          </form>
        )}

        {tab === 'key' && (
          <form
            className="apanel"
            onSubmit={(e) => {
              e.preventDefault()
              void submitKey(e.currentTarget)
            }}
          >
            <label htmlFor="tokInput" className="vh">
              API key or operator token
            </label>
            <input
              ref={firstFieldRef}
              id="tokInput"
              name="token"
              type="password"
              placeholder="rya_sk_… or operator token"
              aria-label="API key"
            />
            <button className="btn dark" type="submit" disabled={busy}>
              <Plug aria-hidden="true" focusable="false" />
              Connect
            </button>
          </form>
        )}

        {tab === 'ws' && (
          <div className="apanel">
            {issuedKey ? (
              <>
                <div className="keynote">
                  Workspace <span className="mono">{issuedKey.name}</span> ready. API key (saved in
                  this browser — copy it somewhere safe):
                  <br />
                  <span className="mono">{issuedKey.key}</span>
                </div>
                <button className="btn dark" onClick={() => enterWithKey(issuedKey.key)}>
                  <ArrowRight aria-hidden="true" focusable="false" />
                  Open console
                </button>
              </>
            ) : (
              <>
                <div className="sub" style={{ margin: '0 0 10px' }}>
                  {workspaces?.length
                    ? 'Your workspaces — open one, or invite a teammate:'
                    : 'No workspaces yet.'}
                </div>
                {workspaces?.map((w) => (
                  <div className="wsrow" key={w.id}>
                    <span className="nm">{w.name}</span>
                    <span className="ptag">{w.role || 'owner'}</span>
                    <button className="btn" onClick={() => void openWorkspace(w)} disabled={busy}>
                      Open
                    </button>
                    {w.role !== 'member' && (
                      <button
                        className="btn"
                        onClick={() => void invite(w)}
                        title="Invite a teammate by email"
                        disabled={busy}
                      >
                        Invite
                      </button>
                    )}
                  </div>
                ))}
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    void createWorkspace(e.currentTarget)
                  }}
                >
                  <input name="name" placeholder="New workspace name" aria-label="New workspace name" style={{ marginTop: 6 }} />
                  <button className="btn dark" type="submit" disabled={busy}>
                    <Plus aria-hidden="true" focusable="false" />
                    New workspace
                  </button>
                </form>
              </>
            )}
          </div>
        )}

        <div className="aerr">{err}</div>
      </div>
    </div>
  )
}
