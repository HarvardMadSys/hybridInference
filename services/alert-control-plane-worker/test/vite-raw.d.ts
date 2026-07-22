// Vite serves `?raw` imports as the file's textual content. Used by the
// import-boundary test to inspect notification.ts source statically.
declare module "*?raw" {
  const content: string;
  export default content;
}
