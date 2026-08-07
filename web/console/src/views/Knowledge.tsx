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

  const { data, error, loading } = useLoad<SearchResponse | null>(
    () =>
      submitted
        ? api<SearchResponse>(ag(state.agent.name, '/knowledge/search'), {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ query: submitted, limit: LIMIT }),
          })
        : Promise.resolve(null),
    // The agent too: a query result belongs to one agent's knowledge base, and
    // switching agents must not leave the previous one's hits on screen.
    [submitted, state.agent.name],
  )

  // A failed search is worth a toast, but it must not replace the document list: the
  // view is still telling the truth about what is ingested. In an effect rather than
  // inline, because toasting is the parent's state and firing it during render is how
  // a render loop starts. `onToast` is deliberately not a dependency — a parent that
  // re-creates the callback would otherwise re-announce a stale failure.
  useEffect(() => {
    if (error && submitted) onToast(`Search failed — ${error}`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [error, submitted])

  function submit() {
    setSubmitted(query.trim())
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
