import type { Health } from "../api/types";
import type { Theme } from "../hooks/useTheme";
import { LogoMark, MenuIcon, MoonIcon, SourcesIcon, SunIcon } from "./icons";

interface Props {
  health: Health | null;
  theme: Theme;
  onToggleTheme: () => void;
  onToggleSidebar: () => void;
  onToggleSources: () => void;
}

export function Header({ health, theme, onToggleTheme, onToggleSidebar, onToggleSources }: Props) {
  return (
    <header className="header">
      <button className="icon-button only-narrow" onClick={onToggleSidebar} aria-label="Show conversations">
        <MenuIcon />
      </button>
      <div className="brand">
        <LogoMark />
        <div>
          <div className="brand-name">Pipeline &amp; OT Assistant</div>
          <div className="brand-tagline">Answers from approved regulations and guidance</div>
        </div>
      </div>
      <div className="header-actions">
        {health?.offline_answers && (
          <span className="badge badge-warning" title="RAG_ANSWER_MODEL=local-extractive: passages, not written answers">
            Offline mode
          </span>
        )}
        <button className="icon-button only-medium" onClick={onToggleSources} aria-label="Show sources">
          <SourcesIcon />
        </button>
        <button
          className="icon-button"
          onClick={onToggleTheme}
          aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          title={theme === "dark" ? "Light theme" : "Dark theme"}
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </button>
      </div>
    </header>
  );
}
