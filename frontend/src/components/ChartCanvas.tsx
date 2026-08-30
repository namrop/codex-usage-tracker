import { useEffect, useRef } from "react";
import { chartConstructor, replaceChartData, type ChartConfig, type ChartInstance } from "../lib/chart";

interface ChartCanvasProps {
  id: string;
  ariaLabel: string;
  role?: string;
  ariaDescribedBy?: string;
  // null config = destroy the chart and hide the canvas (the pre-React
  // destroy-and-hide path for unavailable subscription history).
  config: ChartConfig | null;
  hidden?: boolean;
  // Reports whether Chart.js actually drew — the caller appends its
  // "drawing unavailable; the accessible data table remains current" note.
  onRendered?: (rendered: boolean) => void;
}

export function ChartCanvas({ id, ariaLabel, role, ariaDescribedBy, config, hidden, onRendered }: ChartCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<ChartInstance | undefined>(undefined);

  useEffect(() => {
    const canvas = canvasRef.current;
    const report = (rendered: boolean) => {
      if (onRendered) onRendered(rendered);
    };
    if (!canvas || !config) {
      if (chartRef.current) {
        chartRef.current.destroy();
        chartRef.current = undefined;
      }
      report(false);
      return;
    }
    const Ctor = chartConstructor();
    if (!Ctor) {
      report(false);
      return;
    }
    try {
      if (!chartRef.current) {
        chartRef.current = new Ctor(canvas, {
          type: config.type,
          data: { labels: config.data.labels, datasets: config.data.datasets },
          options: config.options,
        });
      } else {
        replaceChartData(chartRef.current, config.data.labels, config.data.datasets, config.options);
      }
      report(true);
    } catch (error) {
      if (chartRef.current) {
        chartRef.current.destroy();
        chartRef.current = undefined;
      }
      report(false);
    }
  }, [config, onRendered]);

  useEffect(() => () => {
    if (chartRef.current) {
      chartRef.current.destroy();
      chartRef.current = undefined;
    }
  }, []);

  return (
    <canvas
      ref={canvasRef}
      id={id}
      aria-label={ariaLabel}
      role={role}
      aria-describedby={ariaDescribedBy}
      hidden={hidden}
    />
  );
}
