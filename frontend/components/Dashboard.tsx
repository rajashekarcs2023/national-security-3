"use client";

import { useStore } from "@/lib/store";
import { useEdgeWebSocket } from "@/lib/ws";
import Header from "./Header";
import MetricsBar from "./MetricsBar";
import SpectrogramCanvas from "./SpectrogramCanvas";
import IntelligenceEvents from "./IntelligenceEvents";
import EdgeStatusPanel from "./EdgeStatusPanel";
import SignalFeed from "./SignalFeed";
import CustodyTimeline from "./CustodyTimeline";
import CrossSensorPanel from "./CrossSensorPanel";
import CotPublicationsPanel from "./CotPublicationsPanel";
import CoaPanel from "./CoaPanel";
import CommandControls from "./CommandControls";
import ScenarioPanel from "./ScenarioPanel";
import MapPanel from "./MapPanel";
import GeoMapPanel from "./GeoMapPanel";
import AfterActionBrief from "./AfterActionBrief";
import AttributionPanel from "./AttributionPanel";
import PersistencePanel from "./PersistencePanel";
import FoundryStatus from "./FoundryStatus";

export default function Dashboard() {
  // Connect to the edge backend WebSocket
  useEdgeWebSocket();
  const wsConnected = useStore((s) => s.wsConnected);

  return (
    <div className="min-h-screen bg-panel-950 text-slate-200">
      <Header />
      <main className="mx-auto w-full max-w-[1920px] px-4 pb-10 pt-4">
        {!wsConnected && (
          <div className="mb-4 rounded-md border border-accent-amber/40 bg-accent-amber/10 px-4 py-2 text-sm text-accent-amber">
            Connecting to edge node at 127.0.0.1:8765 ...
            <span className="ml-2 text-slate-400">
              (make sure <code className="font-mono">uvicorn app.main:app --port 8765</code> is running)
            </span>
          </div>
        )}
        <MetricsBar />
        <div className="mt-4 grid grid-cols-1 gap-4 2xl:grid-cols-12">
          <div className="2xl:col-span-5 space-y-4">
            <SpectrogramCanvas />
            <GeoMapPanel />
            <EdgeStatusPanel />
            <MapPanel />
          </div>
          <div className="2xl:col-span-4 space-y-4">
            <IntelligenceEvents />
            <AttributionPanel />
            <PersistencePanel />
            <CustodyTimeline />
            <CrossSensorPanel />
          </div>
          <div className="2xl:col-span-3 space-y-4">
            <CommandControls />
            <ScenarioPanel />
            <CoaPanel />
            <FoundryStatus />
            <CotPublicationsPanel />
            <AfterActionBrief />
            <SignalFeed />
          </div>
        </div>
      </main>
    </div>
  );
}
