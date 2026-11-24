// The app icon's amphora, redrawn for the 24-unit grid: a solid vase with two
// bands, which is the level of detail the icon itself uses at small sizes.
const BODY =
  "M15.16 2.4L14.2 3.28L13.79 4.16L13.76 5.05L13.87 5.93L14.29 6.81L15.53 7.69L16.93 8.57L18.09 9.45L18.81 10.34L19.27 11.22L19.49 12.1L4.51 12.1L4.73 11.22L5.19 10.34L5.91 9.45L7.07 8.57L8.47 7.69L9.71 6.81L10.13 5.93L10.24 5.05L10.21 4.16L9.8 3.28L8.84 2.4Z" +
  "M19.36 13.46L19.06 14.23L18.61 15.01L18.04 15.79L5.96 15.79L5.39 15.01L4.94 14.23L4.64 13.46Z" +
  "M16.89 17.14L16.03 18.08L15.19 19.01L14.4 19.94L14.17 20.87L15.29 21.8L8.71 21.8L9.83 20.87L9.6 19.94L8.81 19.01L7.97 18.08L7.11 17.14Z";
const HANDLES = "M9.9 3.6C5.6 3.7 4.6 7 6.6 9.9M14.1 3.6C18.4 3.7 19.4 7 17.4 9.9";

export function BrandMark({ className }: { className?: string }) {
  return (
    <span className={className ? `brand-mark ${className}` : "brand-mark"} aria-hidden="true">
      <svg viewBox="0 0 24 24" focusable="false">
        <path className="brand-mark-body" d={BODY} />
        <path d={HANDLES} />
      </svg>
    </span>
  );
}
