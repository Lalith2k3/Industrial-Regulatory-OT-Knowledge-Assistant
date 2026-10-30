import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { citationNumber, linkCitations } from "../lib/citations";

interface Props {
  text: string;
  onCite: (number: number) => void;
  selected: number | null;
  streaming?: boolean;
}

// Markdown is rendered without raw HTML (react-markdown's default), so answer text cannot inject markup.
export function AnswerText({ text, onCite, selected, streaming = false }: Props) {
  const components: Components = {
    a({ href, children }) {
      const number = citationNumber(href);
      if (number === null) {
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      }
      return (
        <button
          type="button"
          className={`cite-chip ${selected === number ? "is-selected" : ""}`}
          onClick={() => onCite(number)}
          aria-label={`Show source ${number}`}
        >
          {number}
        </button>
      );
    },
  };
  return (
    <div className={`answer-text ${streaming ? "is-streaming" : ""}`}>
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {linkCitations(text)}
      </Markdown>
    </div>
  );
}
