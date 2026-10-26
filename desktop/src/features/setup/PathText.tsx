/**
 * A file path that truncates in the middle, keeping the drive and the last two
 * folders readable. The full path stays in the text (for copying) and the tooltip.
 */
export function PathText({ path, className = "" }: { path: string; className?: string }) {
  const separator = path.includes("\\") ? "\\" : "/";
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = trimmed.lastIndexOf(separator, trimmed.lastIndexOf(separator) - 1);
  const head = cut > 0 ? path.slice(0, cut) : path;
  const tail = cut > 0 ? path.slice(cut) : "";
  return (
    <span className={`path-text ${className}`.trim()} data-tip={path}>
      <span className="path-text-head">{head}</span>
      {tail ? <span className="path-text-tail">{tail}</span> : null}
    </span>
  );
}
