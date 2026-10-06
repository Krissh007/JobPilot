import { Header, Rail, Tabs, Toolbar } from "./components/Chrome";
import { Drawer } from "./components/Drawer";
import { useMeta } from "./data";
import { useUI } from "./state";
import { Analytics } from "./views/Analytics";
import { Answers } from "./views/Answers";
import { Board } from "./views/Board";
import { Database } from "./views/Database";
import { Jobs } from "./views/Jobs";
import { Review } from "./views/Review";
import { Runs } from "./views/Runs";
import { Settings } from "./views/Settings";

export default function App() {
  const { tab } = useUI();
  const { data: meta, isError } = useMeta();
  const threshold = meta?.threshold ?? 60;

  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-3 px-4 pb-10 pt-3 sm:px-5">
      <Header />
      {isError && (
        <div role="alert" className="rounded-lg border border-bad/40 bg-bad-soft px-4 py-2.5 text-[12.5px]">
          <b className="text-bad">The JobPilot API isn't answering.</b> Start it with <code className="font-mono">scripts\run_api.ps1</code> (http://127.0.0.1:8000), then this page reconnects by itself.
        </div>
      )}
      <Rail />
      <div className="flex flex-wrap items-end justify-between gap-x-4 border-b border-line">
        <Tabs />
        {(tab === "board" || tab === "jobs") && <div className="pt-2"><Toolbar threshold={threshold} /></div>}
      </div>
      <main>
        {tab === "board" && <Board threshold={threshold} />}
        {tab === "jobs" && <Jobs threshold={threshold} />}
        {tab === "review" && <Review threshold={threshold} />}
        {tab === "runs" && <Runs />}
        {tab === "answers" && <Answers />}
        {tab === "analytics" && <Analytics />}
        {tab === "database" && <Database />}
        {tab === "settings" && <Settings />}
      </main>
      <Drawer threshold={threshold} />
    </div>
  );
}
