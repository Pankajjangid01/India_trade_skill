import { FormEvent, useEffect, useState } from "react";
import { api } from "../services/api";
import { AlertBanner, Badge, Card, Empty, timeIST } from "../components/ui";

type Arg = { key: string; label: string; type?: "text" | "number" | "checkbox" | "select"; options?: string[]; required?: boolean };

const IDX = ["NIFTY", "SENSEX", "BANKNIFTY"];
const TICKET = { key: "id", label: "Ticket id (T20261126-01)", required: true };
// the arguments each command takes (the backend checks every value again)
const ARGS: Record<string, Arg[]> = {
  "in-entry check": [{ key: "symbol", label: "Index", type: "select", options: IDX, required: true }],
  "in-scanner orb": [{ key: "stocks", label: "Add F&O stocks", type: "checkbox" }],
  "in-scanner daily": [{ key: "stocks", label: "Add F&O stocks", type: "checkbox" }],
  "in-news run": [{ key: "symbols", label: "Symbols (comma list, optional)" }, { key: "hours", label: "Hours", type: "number" }],
  "in-ticket show": [{ key: "id", label: "Ticket id (optional)" }],
  "in-ticket fill": [TICKET, { key: "price", label: "Fill price", type: "number", required: true },
    { key: "time", label: "Time HH:MM", required: true }, { key: "qty", label: "Qty (if partial)", type: "number" }],
  "in-ticket close": [TICKET, { key: "price", label: "Exit price", type: "number", required: true },
    { key: "time", label: "Time HH:MM", required: true },
    { key: "reason", label: "Reason", type: "select", options: ["stop", "target", "flat_by", "no_progress", "manual"], required: true }],
  "in-ticket skip": [TICKET],
  "in-flows show": [{ key: "days", label: "Days", type: "number" }],
  "in-analyst add": [{ key: "symbol", label: "Symbol", required: true }, { key: "quarter", label: "Quarter (Q2FY27)", required: true },
    { key: "sales", label: "Sales Rs cr", type: "number", required: true }, { key: "ebitda", label: "EBITDA Rs cr", type: "number", required: true },
    { key: "profit", label: "Net profit Rs cr", type: "number", required: true }, { key: "eps", label: "EPS Rs", type: "number" },
    { key: "source", label: "Source" }],
  "in-analyst show": [{ key: "symbol", label: "Symbol", required: true }],
  "in-analyst checklist": [{ key: "symbol", label: "Symbol", required: true }],
  "in-risk check": [{ key: "ticket", label: "Ticket id (optional)" }],
  "in-exit plan": [{ key: "stop", label: "Stop (or-mid, pct:0.4, atr:1.5)" }, { key: "entry_premium", label: "Entry premium", type: "number" }],
  "in-timemachine run": [{ key: "symbol", label: "Index", type: "select", options: ["", ...IDX] },
    { key: "days", label: "Days (20 to 100)", type: "number" }, { key: "stop", label: "Stop rule" }],
};

const Runner = ({ skill, command, info, onDone }: { skill: string; command: string; info: any; onDone: () => void }) => {
  const spec = ARGS[`${skill} ${command}`] || [];
  const [vals, setVals] = useState<Record<string, any>>({});
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  const show = async () => {
    setErr(null);
    try {
      setOut(await api.lastSkill(skill, command));
    } catch (e: any) {
      setErr(e.message);
    }
  };
  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const args: Record<string, unknown> = {};
      for (const a of spec) {
        const v = vals[a.key];
        if (v === undefined || v === "" || v === false) continue;
        args[a.key] = a.type === "number" ? Number(v) : v;
      }
      setOut(await api.runSkill(skill, command, args));
      onDone();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const inp = "border border-slate-300 rounded-md px-2 py-1 text-sm bg-white";
  return (
    <div className="border-t border-slate-100 py-3">
      <form onSubmit={run} className="flex flex-wrap items-end gap-2">
        <div className="min-w-[220px]">
          <div className="font-medium text-sm text-slate-900">{command}</div>
          <div className="text-xs text-slate-500">{info.description}</div>
        </div>
        {spec.map((a) => (
          <label key={a.key} className="text-xs text-slate-500 flex flex-col">
            {a.label}
            {a.type === "select" ? (
              <select className={inp} required={a.required} value={vals[a.key] ?? ""} onChange={(e) => setVals({ ...vals, [a.key]: e.target.value })}>
                {!a.required && <option value="">—</option>}
                {a.required && <option value="" disabled>choose</option>}
                {a.options!.filter((o) => o).map((o) => <option key={o}>{o}</option>)}
              </select>
            ) : a.type === "checkbox" ? (
              <input type="checkbox" checked={!!vals[a.key]} onChange={(e) => setVals({ ...vals, [a.key]: e.target.checked })} />
            ) : (
              <input className={inp} required={a.required} type={a.type === "number" ? "number" : "text"} step="any"
                value={vals[a.key] ?? ""} onChange={(e) => setVals({ ...vals, [a.key]: e.target.value })} />
            )}
          </label>
        ))}
        <button type="submit" disabled={busy} className="px-3 py-1.5 rounded-md bg-slate-900 text-white text-xs font-medium disabled:opacity-50">
          {busy ? "Running…" : "Run"}
        </button>
        {info.last && (
          <button type="button" onClick={show} className="px-3 py-1.5 rounded-md border border-slate-300 text-xs">
            Last run
          </button>
        )}
        {info.last && (
          <span className="text-xs text-slate-500">
            <Badge tone={info.last.ok ? "green" : "amber"}>{info.last.ok ? "ok" : "nothing / refused"}</Badge>{" "}
            {timeIST(info.last.started_ist)}
          </span>
        )}
      </form>
      {err && <p className="text-xs text-red-600 mt-2">{err}</p>}
      {out && (
        <div className="mt-2">
          <p className="text-xs text-slate-500 mb-1">
            exit {out.data.exit_code} in {out.data.seconds} s, {timeIST(out.data.started_ist)}
            {Object.keys(out.data.outputs || {}).length > 0 && ` · wrote ${Object.keys(out.data.outputs).join(", ")}`}
          </p>
          <pre className="text-xs bg-slate-900 text-slate-100 rounded-lg p-3 overflow-x-auto max-h-96 whitespace-pre-wrap">
            {(out.data.stdout || "") + (out.data.stderr ? `\n${out.data.stderr}` : "")}
          </pre>
        </div>
      )}
    </div>
  );
};

export const SkillsPage = () => {
  const [cat, setCat] = useState<Record<string, any> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.skills().then(setCat).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
  }, []);
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 mb-5">Skill pack</h1>
      <AlertBanner
        message="Every skill's one-shot command, run on the backend's trader folder. Analysis only: nothing here places an order. The engine also runs in-news every trading morning and in-alerts every minute while a trade you filled by hand is open."
      />
      {err && <AlertBanner type="error" message={err} />}
      {!cat ? (
        <Empty>Loading…</Empty>
      ) : (
        Object.entries(cat).map(([skill, cmds]) => (
          <Card key={skill} title={skill}>
            {Object.entries(cmds as Record<string, any>).map(([cmd, info]) => (
              <Runner key={cmd} skill={skill} command={cmd} info={info} onDone={load} />
            ))}
          </Card>
        ))
      )}
    </>
  );
};
