import { bucketWording, formatBucketLabel, intFormat } from "../lib/format";

// UTC-bucketed series table (the fold bodies): one row per complete UTC
// bucket (15-minute by default; hourly for an older backend that carries no
// bucket_minutes), one column per series. UTC-labelled bucket tables render
// in UTC, not as raw ISO strings.

export interface SeriesTableRow {
  label: string;
  values: unknown[];
}

export function SeriesTable({ headId, bodyId, caption, bucketStarts, bucketMinutes = 60, rows, formatCell = intFormat, headLabel }: {
  headId: string;
  bodyId: string;
  caption: string;
  bucketStarts: unknown[];
  bucketMinutes?: number;
  rows: SeriesTableRow[];
  formatCell?: (value: unknown) => string;
  headLabel?: (row: SeriesTableRow) => string;
}) {
  const wording = bucketWording(bucketMinutes);
  return (
    <div className="table-wrap">
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr id={headId}>
            <th scope="col">{wording.header}</th>
            {rows.map((row, index) => (
              <th scope="col" key={`${row.label}-${index}`}>{headLabel ? headLabel(row) : row.label}</th>
            ))}
          </tr>
        </thead>
        <tbody id={bodyId}>
          {bucketStarts.map((startedAt, index) => (
            <tr key={String(startedAt)}>
              <th scope="row">{formatBucketLabel(startedAt, bucketMinutes)}</th>
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
