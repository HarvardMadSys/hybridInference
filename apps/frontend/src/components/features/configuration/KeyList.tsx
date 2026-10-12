/** Setting names, comma-separated, each in code type. */
export function KeyList({ keys }: { keys: readonly string[] }) {
  return (
    <>
      {keys.map((key, index) => (
        <span key={key}>
          {index > 0 ? ', ' : null}
          <code className="font-mono">{key}</code>
        </span>
      ))}
    </>
  );
}
