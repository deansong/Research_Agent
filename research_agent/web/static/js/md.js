// A small Markdown renderer for the discussor's words: headings, lists,
// code blocks, paragraphs, **bold**, *italics*, `code` and http(s) links.
// Everything is escaped first, so no HTML from a model reaches the page.

export function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function inline(text) {
  let out = escapeHtml(text);
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  out = out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  return out;
}

export function markdown(text) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let list = null;     // "ul" or "ol" while inside one
  let para = [];
  const flush = () => {
    if (para.length) html.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  const close = () => { if (list) { html.push(`</${list}>`); list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      flush(); close();
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith("```"); i++) code.push(lines[i]);
      html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (heading) {
      flush(); close();
      html.push(`<h4>${inline(heading[2])}</h4>`);
    } else if (bullet || number) {
      flush();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { close(); html.push(`<${kind}>`); list = kind; }
      html.push(`<li>${inline((bullet || number)[1])}</li>`);
    } else if (!line.trim()) {
      flush(); close();
    } else {
      close();
      para.push(line);
    }
  }
  flush(); close();
  return html.join("");
}
