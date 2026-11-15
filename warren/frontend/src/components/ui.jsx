import React from "react";

// ---- small shared UI pieces -------------------------------------------------
export function Logo({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-label="Warren">
      <rect width="32" height="32" rx="8" fill="#f26a2e" />
      <ellipse cx="12.4" cy="10" rx="2.5" ry="6.2" fill="#fff" transform="rotate(-12 12.4 10)" />
      <ellipse cx="19.6" cy="10" rx="2.5" ry="6.2" fill="#fff" transform="rotate(12 19.6 10)" />
      <ellipse cx="16" cy="20.5" rx="7.2" ry="6" fill="#fff" />
      <circle cx="13.5" cy="19.6" r="1" fill="#f26a2e" />
      <circle cx="18.5" cy="19.6" r="1" fill="#f26a2e" />
    </svg>
  );
}

const ICONS = {
  search: "M11 19a8 8 0 1 1 5.3-14 8 8 0 0 1-5.3 14zm10 2-4.3-4.3",
  bell: "M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9zm4.3 13a1.9 1.9 0 0 0 3.4 0",
  filter: "M22 3H2l8 9.5V19l4 2v-8.5z",
  sliders: "M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M1 14h6m2-6h6m2 8h6",
  userplus: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m6-10a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm11 0v6m3-3h-6",
  warning: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4m0 4h.01",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  github: "M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22",
  layers: "m12 2 10 5-10 5L2 7zm10 10-10 5-10-5m20 5-10 5-10-5",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  home: "m3 10 9-7 9 7v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM9 22V12h6v10",
  radio: "M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2zM7.8 16.2a6 6 0 0 1 0-8.4m8.4 0a6 6 0 0 1 0 8.4M4.900 19.100a10 10 0 0 1 0-14.200m14.200 0a10 10 0 0 1 0 14.200",
  db: "M12 8c4.400 0 8-1.300 8-3s-3.600-3-8-3-8 1.300-8 3 3.600 3 8 3zm8 0v4c0 1.700-3.600 3-8 3s-8-1.300-8-3V8m16 4v4c0 1.700-3.600 3-8 3s-8-1.300-8-3v-4",
  flask: "M10 2v7.300L4.500 19a2 2 0 0 0 1.700 3h11.600a2 2 0 0 0 1.700-3L14 9.300V2M8.500 2h7M7 16h10",
  sparkle: "M12 3l1.900 5.800L20 10l-6.100 1.200L12 17l-1.900-5.800L4 10l6.100-1.200z",
  refresh: "M21 12a9 9 0 0 1-15.500 6.200M3 12A9 9 0 0 1 18.500 5.800M21 3v6h-6M3 21v-6h6",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6m4-3h6v6m-11 5L21 3",
  send: "m22 2-7 20-4-9-9-4zm0 0L11 13",
  play: "M5 3l14 9-14 9z",
  wrench: "M14.700 6.300a1 1 0 0 0 0 1.400l1.600 1.600a1 1 0 0 0 1.400 0l3.800-3.800a6 6 0 0 1-7.900 7.900l-6.900 6.900a2.100 2.100 0 0 1-3-3l6.900-6.900a6 6 0 0 1 7.900-7.900z",
};

export function Icon({ name, size = 16, className = "" }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[name]} />
    </svg>
  );
}

export const Spinner = ({ size = 14 }) => <span className="spinner" style={{ width: size, height: size }} />;

export function Pill({ tone = "", children, title }) {
  return <span className={`pill ${tone}`} title={title}>{children}</span>;
}

export function Avatar({ src, name = "?", size = 22, square = false, className = "" }) {
  const style = { width: size, height: size, borderRadius: square ? 8 : "50%" };
  return src ? <img className={`avatar ${className}`} style={style} src={src} alt={name} title={name} />
    : <span className={`avatar fallback ${className}`} style={style} title={name}>{name[0]?.toUpperCase()}</span>;
}

export const githubAvatar = (login) => `https://github.com/${login}.png?size=48`;

export function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(1, Math.floor((Date.now() - new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").getTime()) / 1000));
  if (s < 60) return "just now";
  const units = [[86400, "d"], [3600, "h"], [60, "m"]];
  for (const [secs, label] of units) if (s >= secs) return `${Math.floor(s / secs)}${label} ago`;
  return "";
}

// ---- tiny markdown renderer (bold, code, headings, lists) ---------------------
const inline = (text) =>
  text.split(/(\*\*[^*]+\*\*|`[^`]+`|_[^_]+_)/g).filter(Boolean).map((part, i) => {
    if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`")) {
      const code = part.slice(1, -1);
      return <code key={i} className={/^[0-9a-f]{7,}$/.test(code) ? "sha" : ""}>{code}</code>;
    }
    if (part.startsWith("_") && part.endsWith("_") && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>;
    return part;
  });

export function Markdown({ text = "" }) {
  const clean = text.replace(/<!--[\s\S]*?-->/g, "").replace(/<details>[\s\S]*?<\/details>/g, "").replace(/<\/?sub>/g, "").trim();
  const blocks = clean.split(/\n{2,}/);
  return (
    <div className="md">
      {blocks.map((block, i) => {
        if (block.startsWith("```")) return <pre key={i}>{block.replace(/```\w*\n?/g, "")}</pre>;
        if (/^#{1,3} /.test(block)) return <h4 key={i}>{inline(block.replace(/^#+ /, ""))}</h4>;
        if (/^[-*] /.test(block)) return <ul key={i}>{block.split("\n").map((l, j) => <li key={j}>{inline(l.replace(/^[-*] /, ""))}</li>)}</ul>;
        return <p key={i}>{inline(block)}</p>;
      })}
    </div>
  );
}

export const SEVERITY_TONE = { critical: "sev-critical", major: "sev-major", high: "sev-major", minor: "sev-minor", medium: "sev-minor", nitpick: "sev-low", low: "sev-low" };
