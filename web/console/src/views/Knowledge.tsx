import { useEffect, useState } from 'react'
import { BookOpenText, Search } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { stamp } from '../lib/format'
import type { ConsoleState } from '../lib/types'
import { Empty, SecRow, Table, ViewHeader } from '../components/ui'

/**
 * One ingested document, as `ctx.knowledge.add` writes it (`sdk/context.py`) and
 * `snapshot.py` passes it through untouched. Declared here rather than widening
 * `lib/types.ts: Knowledge`, whose `documents` entries model only `{id, title,
 * chunks}`. `source` is the caller's label and is optional by design — `add(text)`
 * with no source is legal, which is why the table falls back to the id.
 */
interface KnowledgeDoc {
  id: string
  source?: string | null
  chunks?: number
  chars?: number
  createdAt?: string
  title?: string
}

/** `POST /agents/{agent}/knowledge/search` — `api/app.py: knowledge_search`. */
interface SearchHit {
  text: string
  source?: string | null
  docId?: string | null
  /** Vector similarity blended with lexical overlap; the underscore is the server's. */
  _score: number
}

interface SearchResponse {
  query: string
  hits: SearchHit[]
}

const LIMIT = 5
/** Chunks are long; the hit list is for recognising a match, not for reading it. */
const PREVIEW = 300

export function KnowledgeView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const k = state.knowledge
  const docs = (k?.documents ?? []) as KnowledgeDoc[]
  const chunks = k?.chunks ?? 0

  /**
   * The query box is React state, and that is the whole point of this migration.
   *
   * The legacy console read the value straight off the DOM (`kqInput.value`) and its
   * 6s poll rebuilt the panel around it from an HTML string, so it had to sniff
   * `document.activeElement` to avoid eating a half-typed query and the caret with
   * it. Here the input is controlled and a poll arrives as new props: it cannot
   * reach the value, the focus or the selection. No activeElement check exists in
   * this file, and none should be added.
   */
  const [query, setQuery] = useState('')

  /**
   * The SUBMITTED query, kept separate from what is being typed.
   *
   * Search is on demand — Enter or the button — not on every keystroke, so `submitted`
   * is what `useLoad` keys off. An empty string resolves to `null` without a request,
   * which is how "on entry" behaves: landing on the view lists documents and asks the
   * server nothing.
   */
  const [submitted, setSubmitted] = useState('')

  /**
   * The "go" signal, kept separate from the query TEXT — a monotonic counter.
   *
   * §5.16: `submitted` was doing both jobs at once, and the two want opposite things.
   * A value DEDUPLICATES by design — `setSubmitted('refunds')` while `submitted` is
   * already `'refunds'` is a no-op, React bails out, `useLoad`'s deps never change and
   * no request goes out at all. An event must not deduplicate. So pressing Search a
   * second time on the same term did nothing whatsoever: no fetch, no spinner, nothing
   * on screen. The two cases where an operator does exactly that are retrying a failed
   * search, and re-running a query after ingesting a document — which is the single
   * most likely reason to press it twice.
   *
   * A counter in the dependency list is the console's existing idiom for "this
   * happened again": it is the shape of the shell's refresh signal (`lib/refresh.ts`),
   * which `useLoad` already folds into these same deps. Bumping it keeps ONE code path
   * for "run a search" — a new query and a repeat are the same submit — rather than a
   * separate escape hatch (`reload()`) for the repeat, which would be a second way to
   * start a search for the next change to forget about.
   */
  const [searchTick, setSearchTick] = useState(0)

  const { data, error, loading } = useLoad<SearchResponse | null>(
    () =>
      submitted
        ? api<SearchResponse>(ag(state.agent.name, '/knowledge/search'), {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ query: submitted, limit: LIMIT }),
          })
        : Promise.resolve(null),
    // `searchTick` is what makes a REPEAT search actually re-run. `submitted` stays
    // beside it because it is what the request is made OF, and a dependency list that
    // omits a value the fetcher reads is a trap for whoever edits this next.
    // The agent too: a query result belongs to one agent's knowledge base, and
    // switching agents must not leave the previous one's hits on screen.
    [submitted, searchTick, state.agent.name],
  )

  /**
   * A failed search is worth a toast, but it must not replace the document list: the
   * view is still telling the truth about what is ingested. In an effect rather than
   * inline, because toasting is the parent's state and firing it during render is how
   * a render loop starts.
   *
   * **The dependency list is the whole subtlety here, and it is the second half of
   * §5.16.** It was `[error, submitted]`, which fails in both directions:
   *
   *  - A retry that fails AGAIN with the same message on the same query changes
   *    neither dep, so the second failure was swallowed in silence — the console
   *    reading as "nothing happened", which is the very complaint §5.16 is about.
   *  - Anything that changes at SUBMIT time — `submitted`, and `searchTick` if it were
   *    listed here — re-runs this effect while `error` still holds the PREVIOUS
   *    attempt's message. `useLoad` clears `error` only on SUCCESS (`reload` opens with
   *    `setLoading(true)` and leaves the standing failure alone until the new one
   *    settles), so submitting a new query after a failure announced the OLD failure
   *    instantly, for a request that had not been made yet.
   *
   * The only honest trigger is therefore the SETTLE, and the only signal for it is
   * `loading` falling back to false — hence the guard. Do not add `searchTick` or
   * `submitted` to these deps to "make it fire more"; that reintroduces the stale
   * toast above. `onToast` is deliberately not a dependency either — a parent that
   * re-creates the callback would otherwise re-announce a failure on its own.
   */
  useEffect(() => {
    if (loading) return
    if (error && submitted) onToast(`Search failed — ${error}`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, error])

  function submit() {
    setSubmitted(query.trim())
    // Unconditional, and that is the fix: an event that only fires when the value
    // changed is not an event.
    setSearchTick((n) => n + 1)
  }

  return (
    <>
      <ViewHeader title="Knowledge">
        RAG — ingested documents, chunked + embedded, that the agent retrieves over with{' '}
        <span className="mono">ctx.knowledge.search</span>.
      </ViewHeader>

      {/*
        `.fsearch` is the console's only search-input class. Its `margin-left:auto`
        is for a trailing filter in a pill row; here the box IS the control, so the
        legacy layout (grow to fill, button beside it) is restored inline rather than
        by adding a class to styles.css.
      */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        <input
          className="fsearch"
          style={{ flex: 1, marginLeft: 0 }}
          placeholder="Search the knowledge base…"
          aria-label="Search the knowledge base"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
        />
        <button className="btn dark sm" onClick={submit} disabled={loading && !!submitted}>
          <Search aria-hidden="true" focusable="false" />
          Search
        </button>
      </div>

      {/*
        Three states, and only one of them is a problem. No query yet: show nothing.
        A query with hits: show them. A query with no hits: say so plainly. An empty
        result set means the corpus does not contain the query, which is an ANSWER —
        rendering it as a failure would send an operator to check the embeddings.
      */}
      {submitted && !error && data && (
        <>
          {data.hits.length === 0 ? (
            <Empty icon={Search}>No matches for “{submitted}”.</Empty>
          ) : (
            <>
              <SecRow left="Top matches" right={`${data.hits.length} · query: ${submitted}`} />
              {data.hits.map((h, i) => (
                // Hits are chunks and the response carries no chunk id, only the
                // document it came from — so the key pairs docId with rank. Safe
                // here in a way it would not be in a polled table: this list is
                // replaced wholesale by the next search and never patched in place.
                <div
                  className="window"
                  style={{ padding: '11px 15px', marginBottom: 8 }}
                  key={`${h.docId ?? h.source ?? 'hit'}-${i}`}
                >
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span className="ptag">{h._score.toFixed(3)}</span>
                    <span className="mono dim" style={{ fontSize: 11 }}>
                      {h.source || h.docId || '—'}
                    </span>
                  </div>
                  <div style={{ marginTop: 6, fontSize: 13, lineHeight: 1.5 }}>
                    {h.text.slice(0, PREVIEW)}
                    {h.text.length > PREVIEW ? '…' : ''}
                  </div>
                </div>
              ))}
            </>
          )}
        </>
      )}

      <SecRow
        left="Documents"
        right={`${docs.length} docs · ${chunks} chunks · vector + lexical recall`}
      />
      <Table
        rows={docs}
        // The document id. `source` is the human label and is neither unique nor
        // required — two `add(text, source='faq.md')` calls are two documents.
        rowKey={(d) => d.id}
        emptyIcon={BookOpenText}
        emptyMessage="No documents — ingest with ctx.knowledge.add(text, source)."
        columns={[
          { header: 'Source', cell: (d) => <span className="mono">{d.source || d.id}</span> },
          { header: 'Chunks', cell: (d) => String(d.chunks ?? 0) },
          { header: 'Size', cell: (d) => `${d.chars ?? 0} ch` },
          {
            header: 'Ingested',
            cell: (d) => <span className="dim">{stamp(d.createdAt) || '—'}</span>,
          },
        ]}
      />
    </>
  )
}
