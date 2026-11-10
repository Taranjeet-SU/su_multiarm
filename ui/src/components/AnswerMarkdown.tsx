import type { Root, RootContent } from "mdast";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { citationParts } from "../lib/citations";
import { displayText, safeHref } from "../lib/text";

const citationUrl = (index: number) => `#answer-citation-${index}`;

/** Decorate parsed prose, never code, HTML or link destinations. IDs are data, not Markdown. */
function remarkCitations(ids: string[]) {
  return (tree: Root) => {
    function walk(node: Root | RootContent) {
      if (!("children" in node) || node.type === "link" || node.type === "linkReference") return;
      const children: RootContent[] = [];
      for (const child of node.children) {
        if (child.type !== "text") {
          walk(child);
          children.push(child);
          continue;
        }
        const parts = citationParts(child.value, ids);
        for (let i = 0; i < parts.length; i++) {
          const part = parts[i];
          if (part.cite) {
            children.push({ type: "link", url: citationUrl(ids.indexOf(part.cite)), children: [{ type: "text", value: part.cite }] });
          } else {
            let value = part.text;
            // [evidence-id] becomes one citation chip, without redundant surrounding brackets.
            if (parts[i + 1]?.cite && value.endsWith("[") && parts[i + 2]?.text.startsWith("]")) value = value.slice(0, -1);
            if (parts[i - 1]?.cite && value.startsWith("]") && parts[i - 2]?.text.endsWith("[")) value = value.slice(1);
            if (value) children.push({ type: "text", value });
          }
        }
      }
      node.children = children as typeof node.children;
    }
    walk(tree);
  };
}

// An allow-list also excludes footnote backreferences and controls. Raw HTML is displayed as text;
// images have only alt text, so a model response cannot initiate an external image request.
const elements = ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "ul", "ol", "li", "blockquote", "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "a", "img"];

export function AnswerMarkdown({ text, citations, onCite }: { text: string; citations: string[]; onCite: (id: string) => void }) {
  const citationLinks = new Map(citations.map((id, i) => [citationUrl(i), { id, number: i + 1 }]));
  return (
    <div className="answer-text" data-testid="answer-text">
      <Markdown
        remarkPlugins={[remarkGfm, [remarkCitations, citations]]}
        allowedElements={elements}
        unwrapDisallowed
        urlTransform={(url) => citationLinks.has(url) ? url : safeHref(url) ?? ""}
        components={{
          a: ({ href, children }) => {
            const cite = href ? citationLinks.get(href) : undefined;
            if (cite) return <button type="button" className="chip cite cite-inline" title={displayText(cite.id)} aria-label={`Show evidence ${displayText(cite.id)}`} onClick={() => onCite(cite.id)}>[{cite.number}]</button>;
            const safe = safeHref(href);
            return safe ? <a href={safe} target="_blank" rel="noopener noreferrer nofollow">{children}</a> : <>{children}</>;
          },
          img: ({ alt }) => <span>{alt}</span>,
          table: ({ children }) => <div className="answer-table-scroll" role="region" aria-label="Answer table" tabIndex={0}><table className="data answer-table">{children}</table></div>,
          // GFM alignment normally creates inline styles, forbidden by the UI's CSP.
          th: ({ children, style }) => <th scope="col" className={`align-${style?.textAlign ?? "left"}`}>{children}</th>,
          td: ({ children, style }) => <td className={`align-${style?.textAlign ?? "left"}`}>{children}</td>,
        }}
      >{displayText(text)}</Markdown>
    </div>
  );
}
