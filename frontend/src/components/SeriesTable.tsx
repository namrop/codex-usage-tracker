import { formatUtcHour, intFormat } from "../lib/format";

// UTC-bucketed series table (the fold bodies): one row per complete UTC
// hour, one column per series. UTC-labelled bucket tables render in UTC,
// not as raw ISO strings.

export interface SeriesTableRow {
  label: string;
  values: unknown[];
}

export function SeriesTable({ headId, bodyId, caption, bucketStarts, rows, formatCell = intFormat, headLabel }: {
  headId: string;
  bodyId: string;
  caption: string;
  bucketStarts: unknown[];
  rows: SeriesTableRow[];
  formatCell?: (value: unknown) => string;
  headLabel?: (row: SeriesTableRow) => string;
}) {
  return (
    <div className="table-wrap">
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr id={headId}>
            <th scope="col">UTC hour</th>
            {rows.map((row, index) => (
              <th scope="col" key={`${row.label}-${index}`}>{headLabel ? headLabel(row) : row.label}</th>
            ))}
          </tr>
        </thead>
        <tbody id={bodyId}>
          {bucketStarts.map((startedAt, index) => (
            <tr key={String(startedAt)}>
              <th scope="row">{formatUtcHour(startedAt)}</th>
              {rows.map((row, rowIndex) => (
                <td key={`${row.label}-${rowIndex}`}>
                  {formatCell(Array.isArray(row.values) ? row.values[index] : 0)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
