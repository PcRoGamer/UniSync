"use client";

import { memo, useCallback, useEffect, useDeferredValue, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  BookOpen,
  Check,
  CheckCircle2,
  CloudDownload,
  Download,
  Edit3,
  ExternalLink,
  FileAudio,
  FileDown,
  FileVideo,
  FolderPlus,
  Layers,
  Loader2,
  Play,
  Plus,
  Puzzle,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  Waves,
  Zap,
  FastForward,
  ArrowDown,
} from "lucide-react";
import { apiFetch, apiUrl } from "@/lib/api";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSyncProgress } from "@/hooks/useSyncProgress"; // P2-16 WS sync progress
import { MediaPlayer, MediaProvider, Track, useMediaState, type MediaPlayerInstance } from "@vidstack/react";
import {
  DefaultVideoLayout,
  defaultLayoutIcons,
} from "@vidstack/react/player/layouts/default";
// Plex: vidstack default theme + layout CSS required for controls to render
import "@vidstack/react/player/styles/default/theme.css";
import "@vidstack/react/player/styles/default/layouts/video.css";
import {
  checkEcho360Auth,
  closeLmsTab,
  getConnectorInfo,
  getExpectedConnectorVersion,
  hasEcho360Connector,
  listEcho360Courses,
  listEcho360Recordings,
  openEcho360ThroughLms,
  resolveEcho360Sources,
  waitForEcho360Auth,
} from "@/lib/echo360-userscript";
import {
  checkCanvasAuth,
  fetchCanvasCourseContent,
  fetchCanvasCourseFiles,
  listCanvasCourses,
  resolveCanvasEmbeds,
  type CanvasCourse,
  type CanvasCourseContent,
  type CanvasFilesFetchResult,
  type CanvasProgress,
  type ResolvedCanvasEmbed,
} from "@/lib/canvas-userscript";
import { listKnowledgeBases } from "@/lib/knowledge-api";
import Avatar from "boring-avatars";

type CourseLibraryItem = {
  id: string;
  name: string;
  kb_name: string;
  echo360_course_id: string;
  canvas_course_id: string;
  /** Cached course thumbnail URL — populated by Canvas connector on first sync */
  image_download_url?: string;
  auto_sync: boolean;
  created_at: string;
  last_synced_at?: string | null;
};

/**
 * Server-owned state for the connector's browser-arbitrated sync lease.
 *
 * Reading this from the local API intentionally does not go through the
 * userscript: the owner can be busy or reloading while sync work continues,
 * and the Library must still display the backend's current progress.
 */
type SyncOwnerSnapshot = {
  owner?: {
    owner_id?: string | null;
    syncing?: boolean;
    message?: string;
    has_pending_command?: boolean;
  };
};


type MediaItem = {
  id: string;
  title: string;
  original_name: string;
  content_type: string;
  status: string;
  progress: string;
  created_at: string;
  summary?: string;
  kb_name?: string;
  source?: string;
  source_id?: string;
  course_name?: string;
  series_name?: string;
  series_date?: string;
  position_seconds?: number;
  duration_seconds?: number;
  intro_duration?: number;
  subtitle_offset?: number;
};

type EchoCourse = { id: string; name: string; url: string; code?: string };
type EchoRecording = {
  id: string;
  title: string;
  date: string;
  course_id: string;
  course_name: string;
  has_media: boolean;
  imported: boolean;
};

type WordTiming = {
  text: string;
  start: number;
  end: number;
};

type SegmentTiming = {
  start: number;
  end: number;
  text: string;
  words: WordTiming[];
};

/**
 * Parses raw WebVTT text (with inline <mm:ss.mmm> word tags or plain cues)
 * into structured segment and word timing structures for real-time highlighting.
 */
function parseVttToSegments(vttText: string): SegmentTiming[] {
  const lines = vttText.split("\n");
  const segments: SegmentTiming[] = [];
  let currentStart = 0;
  let currentEnd = 0;
  let inCue = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line.startsWith("WEBVTT") || line.startsWith("NOTE")) {
      inCue = false;
      continue;
    }
    if (line.includes("-->")) {
      const parts = line.split("-->").map((p) => p.trim());
      if (parts.length === 2) {
        currentStart = parseVttTimestamp(parts[0]);
        currentEnd = parseVttTimestamp(parts[1]);
        inCue = true;
      }
      continue;
    }
    if (inCue && currentEnd > currentStart) {
      const words: WordTiming[] = [];
      const wordRegex = /<(\d{2}:\d{2}(?:\.\d{3})?|\d{2}:\d{2}:\d{2}(?:\.\d{3})?)>([^<]+)/g;
      let match: RegExpExecArray | null;
      let lastText = "";
      while ((match = wordRegex.exec(line)) !== null) {
        const wTime = parseVttTimestamp(match[1]);
        const wText = match[2].trim();
        if (wText) {
          words.push({ text: wText, start: wTime, end: wTime + 0.4 });
          lastText += (lastText ? " " : "") + wText;
        }
      }
      if (words.length > 0) {
        for (let wIdx = 0; wIdx < words.length - 1; wIdx++) {
          words[wIdx].end = words[wIdx + 1].start;
        }
        words[words.length - 1].end = currentEnd;
        segments.push({
          start: currentStart,
          end: currentEnd,
          text: lastText,
          words,
        });
      } else {
        const cleanText = line.replace(/<[^>]+>/g, "").trim();
        if (cleanText) {
          const tokens = cleanText.split(/\s+/);
          const dur = currentEnd - currentStart;
          const tokenWords = tokens.map((tok, idx) => ({
            text: tok,
            start: currentStart + (dur * idx) / tokens.length,
            end: currentStart + (dur * (idx + 1)) / tokens.length,
          }));
          segments.push({
            start: currentStart,
            end: currentEnd,
            text: cleanText,
            words: tokenWords,
          });
        }
      }
    }
  }
  return segments;
}

function parseVttTimestamp(ts: string): number {
  const clean = ts.split(" ")[0].trim();
  const parts = clean.split(":").map(parseFloat);
  if (parts.length === 3) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  } else if (parts.length === 2) {
    return parts[0] * 60 + parts[1];
  }
  return parseFloat(clean) || 0;
}

function formatTimestamp(seconds: number): string {
  if (isNaN(seconds) || seconds < 0) return "00:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

const _fmtTimestamp = formatTimestamp;

/**
 * Unified Transcript Sentence Card.
 * Used identically inside both the Media Player overlay and the Live Subtitles Sidepane.
 */
const TranscriptCard = memo(function TranscriptCard({
  segment,
  currentTime,
  isActive = false,
  onSeek,
  overlayMode = false,
}: {
  segment: SegmentTiming;
  currentTime: number;
  isActive?: boolean;
  onSeek?: (time: number) => void;
  overlayMode?: boolean;
}) {
  return (
    <div
      className={`group rounded-xl border transition-all ${
        overlayMode
          ? "border-cyan-500/40 bg-[#090d16]/95 text-white shadow-2xl backdrop-blur-md px-4 py-2" // Nuvio dark fix — overlay text on dark player bg
          : isActive
          ? "border-cyan-500/50 bg-cyan-500/10 shadow-sm p-2.5"
          : "border-transparent bg-white/[0.04] hover:border-white/15 hover:bg-white/[0.08] p-2.5" // Nuvio dark fix — subtle glass cards on dark surface
      }`}
    >
      {/* Sidepane Header: Timestamp & Speaking Pulse (omitted in video overlay for minimal height) */}
      {!overlayMode && (
        <div className="mb-1 flex items-center justify-between text-[10px] text-zinc-500"> {/* Nuvio dark fix — muted timestamp row */}
          <button
            type="button"
            onClick={() => onSeek?.(segment.start)}
            className="font-mono font-semibold text-cyan-500 hover:underline cursor-pointer"
            title="Jump to this sentence"
          >
            {_fmtTimestamp(segment.start)}
          </button>
          {isActive && (
            <span className="flex items-center gap-1 font-medium text-cyan-400">
              <span className="h-1.5 w-1.5 rounded-full bg-cyan-400 animate-pulse" />
              Speaking
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center leading-relaxed text-xs sm:text-sm">
        {overlayMode && isActive && (
          <span className="mr-1.5 inline-flex items-center" title="Speaking">
            <span className="h-1.5 w-1.5 rounded-full bg-cyan-400 animate-pulse" />
          </span>
        )}
        {segment.words && segment.words.length > 0 ? (
          segment.words.map((w, wIdx) => {
            const isWordActive = currentTime >= w.start && currentTime <= w.end;
            const isWordPassed = currentTime > w.end;
            return (
              <span
                key={wIdx}
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation();
                  onSeek?.(w.start);
                }}
                className={`mx-0.5 inline-block rounded px-0.5 transition-colors cursor-pointer ${
                  isWordActive
                    ? "bg-cyan-500/25 text-cyan-400 font-bold" // Nuvio dark fix — active karaoke unchanged
                    : isWordPassed
                    ? overlayMode ? "text-white" : "text-zinc-300" // Nuvio dark fix — passed words readable on dark
                    : overlayMode ? "text-zinc-400/60" : "text-zinc-400/60 hover:text-zinc-200" // Nuvio dark fix — upcoming words dim on dark
                }`}
                title={`Jump to ${w.text} (${w.start.toFixed(1)}s)`}
              >
                {w.text}
              </span>
            );
          })
        ) : (
          <span
            onClick={() => onSeek?.(segment.start)}
            className="cursor-pointer text-zinc-300" // Nuvio dark fix — plain text fallback on dark
          >
            {segment.text}
          </span>
        )}
      </div>
    </div>
  );
});

/**
 * On-Player Live Subtitle Overlay Component.
 * Displays a direct clone of the active sentence card from the sidepane inside the player.
 * Uses Vidstack's internal 60fps clock for smooth real-time word-by-word karaoke tracking.
 */
function KaraokeSubtitleOverlay({
  segments,
  currentTime: propCurrentTime,
  onSeek,
}: {
  segments: SegmentTiming[];
  currentTime?: number;
  onSeek?: (time: number) => void;
}) {
  const mediaCurrentTime = useMediaState("currentTime");
  // Prefer Vidstack's high-frequency clock when rendered inside MediaPlayer
  const currentTime = propCurrentTime !== undefined ? propCurrentTime : mediaCurrentTime;
  const currentTextTrack = useMediaState("textTrack");
  const textTracks = useMediaState("textTracks");
  const controlsVisible = useMediaState("controlsVisible");

  const isCaptionsActive =
    currentTextTrack !== null ||
    (Array.isArray(textTracks)
      ? textTracks.some((t) => t && t.mode === "showing")
      : Boolean(textTracks && (textTracks as any).length > 0 && Array.from(textTracks as any).some((t: any) => t && t.mode === "showing")));

  const activeSegment = segments.find(
    (s) => currentTime >= s.start - 0.05 && currentTime <= s.end + 0.15,
  );

  // If captions are turned off in Vidstack's CC menu or not speaking, hide the overlay completely
  if (!isCaptionsActive || !activeSegment || !activeSegment.text) {
    return null;
  }

  return (
    <div
      className={`pointer-events-auto absolute left-4 right-4 z-10 flex justify-center text-left transition-all duration-200 ${
        controlsVisible ? "bottom-24 sm:bottom-28" : "bottom-12 sm:bottom-14"
      }`}
    >
      <div className="max-w-[92%] sm:max-w-[620px] w-full">
        <TranscriptCard
          segment={activeSegment}
          currentTime={currentTime}
          isActive={true}
          onSeek={onSeek}
          overlayMode={true}
        />
      </div>
    </div>
  );
}

/**
 * Floating "Skip Intro" Button & Auto-Skip Controller.
 * Displays during intro disclaimer window (< introDuration) and auto-skips if enabled.
 */
function IntroSkipOverlay({
  introDuration = 0,
  autoSkip = true,
  onSkip,
}: {
  introDuration?: number;
  autoSkip?: boolean;
  onSkip?: () => void;
}) {
  const currentTime = useMediaState("currentTime");
  const controlsVisible = useMediaState("controlsVisible");
  const hasAutoSkippedRef = useRef(false);

  useEffect(() => {
    hasAutoSkippedRef.current = false;
  }, [introDuration]);

  useEffect(() => {
    if (
      autoSkip &&
      introDuration > 0 &&
      currentTime > 0.05 &&
      currentTime < introDuration - 0.5 &&
      !hasAutoSkippedRef.current
    ) {
      hasAutoSkippedRef.current = true;
      onSkip?.();
    }
  }, [autoSkip, introDuration, currentTime, onSkip]);

  if (introDuration <= 0 || currentTime >= introDuration) {
    return null;
  }

  return (
    <div
      className={`absolute right-4 z-30 transition-all duration-200 ${
        controlsVisible ? "bottom-24 sm:bottom-28" : "bottom-14 sm:bottom-16"
      }`}
    >
      <button
        type="button"
        onClick={() => {
          hasAutoSkippedRef.current = true;
          onSkip?.();
        }}
        className="flex items-center gap-2 rounded-full border border-white/25 bg-black/85 px-4 py-2 text-xs font-bold text-white shadow-2xl backdrop-blur-md transition-all hover:bg-cyan-950/90 hover:border-cyan-400/50 hover:scale-105 active:scale-95 cursor-pointer"
      >
        <span>Skip Intro ({Math.round(introDuration)}s)</span>
        <FastForward size={14} className="text-cyan-400" />
      </button>
    </div>
  );
}

/**
 * Interactive Word-by-Word Live Subtitles / Transcript Sidepane.
 * Synchronizes with real-time video/audio playback and allows instant click-to-seek on any word.
 */
function TranscriptSidepane({
  segments,
  currentTime,
  onSeek,
}: {
  segments: SegmentTiming[];
  currentTime: number;
  onSeek: (time: number) => void;
}) {
  const [search, setSearch] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const activeCardRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const touchStartYRef = useRef<number | null>(null);
  const isAutoScrollingRef = useRef(false);

  // Find active segment index
  const activeIndex = segments.findIndex(
    (s) => currentTime >= s.start - 0.05 && currentTime <= s.end + 0.15,
  );

  // Auto-scroll active card into view
  useEffect(() => {
    if (autoScroll && activeCardRef.current && containerRef.current) {
      isAutoScrollingRef.current = true;
      activeCardRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
      const timer = setTimeout(() => {
        isAutoScrollingRef.current = false;
      }, 500);
      return () => clearTimeout(timer);
    }
  }, [activeIndex, autoScroll]);

  // Turn off auto-scroll when user manually wheels/scrolls up
  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY < 0 && autoScroll) {
      setAutoScroll(false);
    }
  };

  // Turn off auto-scroll on mobile upward touch swipe
  const handleTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    touchStartYRef.current = e.touches[0]?.clientY ?? null;
  };

  const handleTouchMove = (e: React.TouchEvent<HTMLDivElement>) => {
    if (touchStartYRef.current !== null && autoScroll) {
      const currentY = e.touches[0]?.clientY ?? touchStartYRef.current;
      // Swiping down moves scroll position UP (reading earlier sentences)
      if (currentY - touchStartYRef.current > 15) {
        setAutoScroll(false);
      }
    }
  };

  const filtered = useMemo(() => {
    if (!search.trim()) return segments.map((s, idx) => ({ segment: s, originalIndex: idx }));
    const q = search.toLowerCase();
    return segments
      .map((s, idx) => ({ segment: s, originalIndex: idx }))
      .filter(({ segment }) => segment?.text && segment.text.toLowerCase().includes(q));
  }, [segments, search]);

  return (
    <div className="relative flex h-full flex-col min-h-0">
      {/* Search & Auto-scroll controls */}
      <div className="mb-2.5 flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" /> {/* Nuvio dark fix */}
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search transcript..."
            className="h-7.5 w-full rounded-lg border border-white/10 bg-white/5 pl-7 pr-6 text-xs text-white placeholder:text-zinc-500 outline-none focus:border-cyan-500/50" // Nuvio dark fix
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-zinc-500 hover:text-white" // Nuvio dark fix
            >
              ✕
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            const next = !autoScroll;
            setAutoScroll(next);
            if (next && activeCardRef.current) {
              activeCardRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
          }}
          className={`inline-flex h-7.5 items-center gap-1 rounded-lg border px-2 text-[11px] font-medium transition cursor-pointer ${
            autoScroll
              ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-400"
              : "border-white/10 bg-white/5 text-zinc-400" // Nuvio dark fix — inactive auto-scroll pill
          }`}
          title="Auto-scroll to the current speaking sentence"
        >
          <span>Auto-scroll</span>
        </button>
      </div>

      {/* Segments List */}
      <div
        ref={containerRef}
        onWheel={handleWheel}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        className="custom-scrollbar relative flex-1 max-h-[380px] sm:max-h-[420px] overflow-y-auto space-y-1.5 pr-1 text-xs"
      >
        {filtered.length === 0 ? (
          /* Plex fix: Split empty states — distinguish "no transcript yet" from "search returned nothing" */
          segments.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center text-xs text-zinc-500 gap-2">
              <FileAudio size={28} className="text-zinc-600" />
              <span>No transcript available</span>
              <span className="text-[11px] text-zinc-600">Subtitles haven't been generated yet. They will appear once processing completes.</span>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-12 text-center text-xs text-zinc-500 gap-2">
              <Search size={20} className="text-zinc-600" />
              <span>No results for &quot;{search.trim()}&quot;</span>
              <button
                type="button"
                onClick={() => setSearch("")}
                className="mt-1 rounded-md border border-white/10 bg-white/5 px-2 py-0.5 text-[10px] text-zinc-400 hover:text-white cursor-pointer" /* Nuvio dark fix */
              >
                Clear search
              </button>
            </div>
          )
        ) : (
          filtered.map(({ segment, originalIndex }) => {
            const isActiveSegment = originalIndex === activeIndex;
            return (
              <div key={originalIndex} ref={isActiveSegment ? activeCardRef : null}>
                <TranscriptCard
                  segment={segment}
                  currentTime={currentTime}
                  isActive={isActiveSegment}
                  onSeek={(time) => {
                    setAutoScroll(true);
                    onSeek(time);
                  }}
                />
              </div>
            );
          })
        )}
      </div>

      {/* Floating Resume Auto-Scroll Button */}
      {!autoScroll && activeIndex >= 0 && (
        <div className="absolute bottom-3 right-3 z-10 animate-fade-in">
          <button
            type="button"
            onClick={() => {
              setAutoScroll(true);
              activeCardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }}
            className="flex items-center gap-1.5 rounded-full border border-cyan-500/40 bg-zinc-950/90 px-3 py-1.5 text-[11px] font-semibold text-cyan-400 shadow-xl backdrop-blur-md transition hover:bg-cyan-950 hover:scale-105 cursor-pointer" // review fix
          >
            <ArrowDown size={12} />
            <span>Resume Auto-scroll</span>
          </button>
        </div>
      )}
    </div>
  );
}


async function responseError(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (body?.detail) {
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) {
        const first = body.detail[0] as { msg?: string } | undefined;
        return first?.msg || fallback;
      }
    }
    return fallback;
  } catch {
    return fallback;
  }
}

function MediaLibraryPageInner() {
  const router = useRouter();
  const [items, setItems] = useState<MediaItem[]>([]);
  const [active, setActive] = useState<MediaItem | null>(null);
  const [activeSeries, setActiveSeries] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState(false);
  const [kbName, setKbName] = useState("");
  const [echoBusy, setEchoBusy] = useState(false);
  const [echoError, setEchoError] = useState("");
  const [echoMessage, setEchoMessage] = useState("");
  const [echoSuccess, setEchoSuccess] = useState("");
  const [echoBrowser, setEchoBrowser] = useState("");
  const [echoWaitingLogin, setEchoWaitingLogin] = useState(false);
  const [echoLoginSecondsLeft, setEchoLoginSecondsLeft] = useState(300);
  const loginAbortControllerRef = useRef<AbortController | null>(null);
  const [echoConnectorInstalled, setEchoConnectorInstalled] = useState<boolean | null>(null);
  const [echoConnectorVersion, setEchoConnectorVersion] = useState<string | null>(null);
  const [echoExpectedVersion, setEchoExpectedVersion] = useState<string | null>(null);
  const [showEchoViewer, setShowEchoViewer] = useState(false);
  const [echoViewerLoaded, setEchoViewerLoaded] = useState(false);
  const [echoViewerKey, setEchoViewerKey] = useState(0);
  const [echoCourses, setEchoCourses] = useState<EchoCourse[]>([]);
  const [courseId, setCourseId] = useState("");
  const [canvasBusy, setCanvasBusy] = useState(false);
  const [canvasError, setCanvasError] = useState("");
  const [canvasMessage, setCanvasMessage] = useState("");
  const [canvasSuccess, setCanvasSuccess] = useState("");
  const [canvasCourses, setCanvasCourses] = useState<CanvasCourse[]>([]);
  const [canvasCourseId, setCanvasCourseId] = useState("");
  const [canvasContent, setCanvasContent] = useState<CanvasCourseContent | null>(null);
  const [canvasImporting, setCanvasImporting] = useState(false);
  const [canvasFilesBusy, setCanvasFilesBusy] = useState(false);
  const [canvasFilesResult, setCanvasFilesResult] = useState<CanvasFilesFetchResult | null>(null);
  const [canvasProgress, setCanvasProgress] = useState<CanvasProgress | null>(null);
  const [canvasLog, setCanvasLog] = useState<string[]>([]);
  const [canvasEmbedsResolving, setCanvasEmbedsResolving] = useState(false);
  const [canvasEmbedsResolved, setCanvasEmbedsResolved] = useState<ResolvedCanvasEmbed[]>([]);
  const [canvasAuthOk, setCanvasAuthOk] = useState<boolean | null>(null);
  const [echoRecordings, setEchoRecordings] = useState<EchoRecording[]>([]);
  const [selectedRecordings, setSelectedRecordings] = useState<Set<string>>(new Set());
  const [libraries, setLibraries] = useState<CourseLibraryItem[]>([]);
  const [showLibModal, setShowLibModal] = useState(false);
  const [editingLib, setEditingLib] = useState<CourseLibraryItem | null>(null);
  const [libId, setLibId] = useState("");
  const [libName, setLibName] = useState("");
  const [libKbName, setLibKbName] = useState("");
  const [libCourseId, setLibCourseId] = useState("");
  const [libCanvasCourseId, setLibCanvasCourseId] = useState("");
  const [libAutoSync, setLibAutoSync] = useState(true);
  const [libSaving, setLibSaving] = useState(false);
  const [activeSegments, setActiveSegments] = useState<SegmentTiming[]>([]);
  const [audioCurrentTime, setAudioCurrentTime] = useState(0);
  const [audioKaraokeMode, setAudioKaraokeMode] = useState<"karaoke" | "standard" | "off">("karaoke");
  const [sidepaneTab, setSidepaneTab] = useState<"transcript" | "overview">("transcript");
  const [autoSkipIntro, setAutoSkipIntro] = useState<boolean>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("unisync_auto_skip_intro") !== "false";
    }
    return true;
  });
  const playerRef = useRef<MediaPlayerInstance>(null);
  const input = useRef<HTMLInputElement>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState("");
  const [syncError, setSyncError] = useState("");
  // Incremented after the connector durably queues a command so the existing
  // status watcher begins polling immediately rather than waiting for a page
  // reload to discover the new owner state.
  const [syncMonitorEpoch, setSyncMonitorEpoch] = useState(0);
  const wsProgress = useSyncProgress(); // P2-16 WS sync progress

  const loadLibraries = useCallback(async () => {
    try {
      const res = await apiFetch(apiUrl("/api/v1/lectures/libraries"), { cache: "no-store" });
      if (res.ok) {
        const data = (await res.json()) as { libraries?: CourseLibraryItem[] };
        setLibraries(data.libraries || []);
      }
    } catch {
      // Ignored
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const response = await apiFetch(apiUrl("/api/v1/lectures"), { cache: "no-store" });
      if (response.ok) {
        setItems(((await response.json()) as { items: MediaItem[] }).items);
      }
    } catch (e) {
      // Backend is likely unreachable; ignore silently to prevent console spam
    }
  }, []);

  /** Queue the whole automated pipeline through the backend source of truth.
   *
   * The owner userscript is still responsible for authenticated browser work,
   * but command submission itself is a local backend write. Sending this hop
   * through postMessage made an already-durable command look unsuccessful when
   * the userscript acknowledgement was lost or the page was GPU-stalled.
   */
  const runSyncNow = useCallback(async () => {
    let monitorQueuedSync = false;
    setSyncing(true);
    setSyncStatus("Starting…");
    setSyncError("");
    try {
      const ownerResponse = await apiFetch(apiUrl("/api/v1/sync/owner"), {
        cache: "no-store",
      });
      if (!ownerResponse.ok) {
        throw new Error("Could not read sync status.");
      }
      const { owner = {} } = (await ownerResponse.json()) as SyncOwnerSnapshot;
      if (!owner.syncing) {
        const queueResponse = await apiFetch(apiUrl("/api/v1/sync/owner/command"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: crypto.randomUUID() }),
        });
        if (!queueResponse.ok) {
          throw new Error("The backend could not queue this sync.");
        }
      }
      monitorQueuedSync = true;
      setSyncStatus(owner.syncing ? owner.message || "Sync already in progress" : "Starting…");
      setSyncMonitorEpoch((epoch) => epoch + 1);
    } catch (err) {
      setSyncError(err instanceof Error ? err.message : "Sync failed.");
      setSyncStatus("");
    } finally {
      // A queued command continues in an owner tab.  The status watcher clears
      // the busy state only after the backend reports its terminal outcome.
      if (!monitorQueuedSync) setSyncing(false);
    }
  }, []);

  /** The sync runs in whichever tab owns the connector's leader lease, so it
   *  keeps going even if this page is reloaded or closed and reopened.  On
   *  mount, ask the connector for the owner's current state and resume showing
   *  it (poll until it finishes). */
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wasSyncing = false;

    const poll = async () => {
      try {
        // The API is the source of truth for a long-running sync. Asking the
        // connector via postMessage here made UI visibility depend on a busy
        // browser userscript replying within five seconds, even though the
        // backend had a healthy owner and current progress.
        const response = await apiFetch(apiUrl("/api/v1/sync/owner"), {
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error("Sync status request failed.");
        }
        const { owner = {} } = (await response.json()) as SyncOwnerSnapshot;
        const pending = Boolean(owner.has_pending_command);
        const isSyncing = Boolean(owner.syncing || pending);
        const message = owner.syncing
          ? owner.message || "Syncing…"
          : pending
            ? owner.owner_id
              ? "Starting…"
              : "Waiting for an open browser tab to start sync…"
            : "";
        if (cancelled) return;
        if (isSyncing) {
          wasSyncing = true;
          setSyncing(true);
          setSyncError("");
          setSyncStatus(message);
          timer = setTimeout(poll, 2000);
        } else {
          setSyncing(false);
          if (wasSyncing) {
            void Promise.all([loadLibraries(), refresh()]);
          }
        }
      } catch {
        if (cancelled) return;
        setSyncing(false);
        setSyncError("Could not read sync status. Check that the backend is running, then retry.");
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadLibraries, refresh, syncMonitorEpoch]);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => {
      void refresh();
      void loadLibraries();
      void listKnowledgeBases().then((kbs) =>
        setKbName((kbs.find((kb) => kb.is_default) ?? kbs[0])?.name ?? ""),
      );
    }, 0);
    return () => window.clearTimeout(initialLoad);
  }, [refresh, loadLibraries]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await apiFetch(apiUrl("/api/v1/lectures/libraries/canvas-auth-check"));
        if (!res.ok) return;
        const data = await res.json() as { authenticated: boolean | null; reason?: string | null };
        if (!cancelled) setCanvasAuthOk(data.authenticated);
      } catch {
      }
    };
    void poll();
    const id = window.setInterval(poll, 60_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  // Auto-provision one KB + course library per enrolled subject whenever the
  // browser reports Canvas/Echo360 enrolments.  Idempotent — the backend
  // matches by subject code and never duplicates.
  useEffect(() => {
    if (!canvasCourses.length && !echoCourses.length) return;
    const t = window.setTimeout(() => {
      void (async () => {
        try {
          await apiFetch(apiUrl("/api/v1/lectures/libraries/auto-register"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              canvas_courses: canvasCourses.map((c) => ({
                id: c.id,
                name: c.name,
                code: c.code || "",
              })),
              echo360_courses: echoCourses.map((c) => ({
                id: c.id,
                name: c.name,
                code: c.code || "",
              })),
            }),
          });
          await loadLibraries();
        } catch {
          // Best-effort; the connector's background auto-sync also retries.
        }
      })();
    }, 800);
    return () => window.clearTimeout(t);
  }, [canvasCourses, echoCourses, loadLibraries]);

  const openCreateLibraryModal = () => {
    setEditingLib(null);
    setLibId("");
    setLibName("");
    setLibKbName("");
    setLibCourseId("");
    setLibCanvasCourseId("");
    setLibAutoSync(true);
    setShowLibModal(true);
  };

  const openEditLibraryModal = (lib: CourseLibraryItem) => {
    setEditingLib(lib);
    setLibId(lib.id);
    setLibName(lib.name);
    setLibKbName(lib.kb_name);
    setLibCourseId(lib.echo360_course_id);
    setLibCanvasCourseId(lib.canvas_course_id || "");
    setLibAutoSync(lib.auto_sync);
    setShowLibModal(true);
  };

  const saveCourseLibrary = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!libId || !libName) return;
    setLibSaving(true);
    try {
      if (editingLib) {
        const res = await apiFetch(apiUrl(`/api/v1/lectures/libraries/${editingLib.id}`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: libName,
            kb_name: libKbName,
            echo360_course_id: libCourseId,
            canvas_course_id: libCanvasCourseId,
            auto_sync: libAutoSync,
          }),
        });
        if (res.ok) {
          setShowLibModal(false);
          await loadLibraries();
        }
      } else {
        const res = await apiFetch(apiUrl("/api/v1/lectures/libraries"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: libId.toLowerCase().replace(/[^a-z0-9_\-]/g, "_"),
            name: libName,
            kb_name: libKbName || `${libId.toLowerCase()}_kb`,
            echo360_course_id: libCourseId,
            canvas_course_id: libCanvasCourseId,
            auto_sync: libAutoSync,
          }),
        });
        if (res.ok) {
          setShowLibModal(false);
          await loadLibraries();
        }
      }
    } catch (err) {
      console.error("Error saving course library:", err);
    } finally {
      setLibSaving(false);
    }
  };

  const deleteCourseLibrary = async (id: string) => {
    if (!confirm(`Are you sure you want to delete course library '${id}'?`)) return;
    try {
      const res = await apiFetch(apiUrl(`/api/v1/lectures/libraries/${id}`), { method: "DELETE" });
      if (res.ok) {
        await loadLibraries();
      }
    } catch (err) {
      console.error("Error deleting course library:", err);
    }
  };

  const deleteMediaItem = async (itemId: string, event?: React.MouseEvent) => {
    if (event) event.stopPropagation();
    try {
      await apiFetch(apiUrl(`/api/v1/lectures/${itemId}`), { method: "DELETE" });
      await refresh();
    } catch (err) {
      console.error("Error deleting media item:", err);
    }
  };

  /**
   * Re-triggers background speech-to-text processing for a media item.
   *
   * Called when a media item is in the 'downloaded' state (e.g. video was fetched
   * successfully but initial STT transcription failed or was skipped).
   * Stops click event propagation to prevent opening the media item viewer card.
   *
   * @param itemId - Unique ID of the lecture media item to reprocess.
   * @param event - Mouse event from the user's click action (optional).
   */
  const reprocessMediaItem = async (itemId: string, event?: React.MouseEvent) => {
    // Prevent click event propagation so clicking the button doesn't trigger parent card selection
    if (event) event.stopPropagation();
    try {
      await apiFetch(apiUrl(`/api/v1/lectures/${itemId}/reprocess`), { method: "POST" });
      // Refresh local media list so the status updates to processing
      await refresh();
    } catch (err) {
      console.error("Error reprocessing media item:", err);
    }
  };


  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // P2-16: WS-driven sync progress — mirror hook state into local UI state
  useEffect(() => {
    setSyncing(wsProgress.is_syncing);
    if (wsProgress.progress?.message) {
      setCanvasProgress({
        phase: "syncing",
        message: wsProgress.progress.message,
        done: wsProgress.progress.done,
        total: wsProgress.progress.total,
      });
    }
    if (wsProgress.log?.length) setCanvasLog(wsProgress.log);
  }, [wsProgress.is_syncing, wsProgress.progress, wsProgress.log]);

  const upload = async (file?: File) => {
    if (!file) return;
    setUploading(true);
    const form = new FormData();
    form.append("file", file);
    form.append("kb_name", kbName);
    const response = await apiFetch(apiUrl("/api/v1/lectures"), {
      method: "POST",
      body: form,
    });
    setUploading(false);
    if (response.ok) await refresh();
  };

  const loadEchoRecordings = useCallback(async (nextCourseId: string) => {
    if (!nextCourseId) return;
    setEchoBusy(true);
    setEchoError("");
    setEchoMessage("Reading the Echo360 course in your browser…");
    try {
      const body = await listEcho360Recordings(nextCourseId);
      const imported = new Set(
        items
          .filter((item) => item.source === "echo360" && item.status !== "failed")
          .map((item) => item.source_id ?? ""),
      );
      const recordings = body.recordings.map((recording) => ({
        ...recording,
        imported: imported.has(recording.id),
      }));
      setEchoRecordings(recordings);
      setSelectedRecordings(
        new Set(
          recordings
            .filter((recording) => !recording.imported && recording.has_media)
            .slice(0, 10)
            .map((recording) => recording.id),
        ),
      );
      setEchoMessage(
        recordings.length
          ? "Ready. The newest 10 unimported lectures are selected."
          : "No recorded lectures were found in this course.",
      );
    } catch (error) {
      setEchoError(
        error instanceof Error ? error.message : "Could not read this Echo360 course.",
      );
      setEchoMessage("");
    } finally {
      setEchoBusy(false);
    }
  }, [items]);

  const fetchEchoCourses = useCallback(async () => {
    setEchoBusy(true);
    setEchoError("");
    setEchoMessage("Loading your UniMelb Echo360 courses in your browser…");
    try {
      const body = await listEcho360Courses();
      setEchoBrowser("your normal browser");
      setEchoCourses(body.courses);
      const firstCourse = body.courses[0]?.id ?? "";
      setCourseId(firstCourse);
      if (firstCourse) await loadEchoRecordings(firstCourse);
      else setEchoMessage("Signed in, but Echo360 returned no current enrolments.");
    } catch (error) {
      setEchoError(error instanceof Error ? error.message : "Could not connect to UniMelb Echo360.");
      setEchoBrowser("");
      setEchoMessage("");
    } finally {
      setEchoBusy(false);
    }
  }, [loadEchoRecordings]);

  /* Auto-connect Echo360 once on mount — also checks version. */
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void (async () => {
        const info = await getConnectorInfo();
        const installed = info?.installed === true;
        setEchoConnectorInstalled(installed);
        if (info?.version) setEchoConnectorVersion(info.version);
        if (installed) {
          const expected = await getExpectedConnectorVersion();
          setEchoExpectedVersion(expected);
        }
        if (!installed) return;
        try {
          const body = await listEcho360Courses();
          setEchoBrowser("your normal browser");
          setEchoCourses(body.courses);
          const firstCourse = body.courses[0]?.id ?? "";
          setCourseId(firstCourse);
          if (firstCourse) await loadEchoRecordings(firstCourse);
        } catch {
          /* stale cookie — user clicks "Connect for import" */
        }
      })();
    }, 100);
    return () => window.clearTimeout(timer);
    // Intentionally runs once on mount so it doesn't re-fire when
    // loadEchoRecordings changes reference (it depends on `items`).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load word-level karaoke timestamps whenever active lecture item changes
  useEffect(() => {
    if (!active?.id) {
      setActiveSegments([]);
      return;
    }
    let cancelled = false;

    async function loadWordTimings() {
      // Plex fix: Try /words first (word-level timing), then /subtitles VTT fallback, then empty.
      // Each failure logs a console.warn so devs can trace transcript availability.
      try {
        const res = await apiFetch(apiUrl(`/api/v1/lectures/${active?.id}/words`));
        if (res.ok) {
          const data = await res.json();
          if (!cancelled && data?.segments && data.segments.length > 0) {
            setActiveSegments(data.segments);
            return;
          }
        } else {
          console.warn(`[Plex fix] /words returned ${res.status} for ${active?.id}`);
        }
      } catch (err) {
        console.warn(`[Plex fix] /words fetch failed for ${active?.id}:`, err);
      }

      // Fallback: raw VTT via /subtitles — parse client-side
      try {
        const subRes = await apiFetch(apiUrl(`/api/v1/lectures/${active?.id}/subtitles`));
        if (subRes.ok) {
          const vttText = await subRes.text();
          const parsed = parseVttToSegments(vttText);
          if (!cancelled && parsed.length > 0) {
            setActiveSegments(parsed);
            return;
          }
        } else {
          console.warn(`[Plex fix] /subtitles returned ${subRes.status} for ${active?.id}`);
        }
      } catch (err) {
        console.warn(`[Plex fix] /subtitles fetch failed for ${active?.id}:`, err);
      }

      // Both failed — set empty so TranscriptSidepane shows "no transcript available"
      if (!cancelled) {
        setActiveSegments([]);
      }
    }

    void loadWordTimings();
    return () => {
      cancelled = true;
    };
  }, [active?.id]);

  /**
   * Cancels the active Echo360 authentication waiting loop and closes the Canvas tab.
   * Invoked when the user clicks 'Cancel' on the login progress banner.
   */
  const cancelEchoLoginWait = useCallback(() => {
    if (loginAbortControllerRef.current) {
      loginAbortControllerRef.current.abort();
      loginAbortControllerRef.current = null;
    }
    setEchoWaitingLogin(false);
    setEchoBusy(false);
    setEchoMessage("UniMelb sign-in wait cancelled.");
    try {
      void closeLmsTab();
    } catch {
      // Best-effort cleanup
    }
  }, []);

  /**
   * Fast-track manual authentication check triggered by the 'Check Now' button.
   * Allows users who just completed 2FA to immediately verify without waiting for next poll cycle.
   */
  const checkAuthNow = useCallback(async () => {
    setEchoMessage("Checking Echo360 session…");
    try {
      const auth = await checkEcho360Auth();
      if (auth.authenticated && auth.courses && auth.courses.length >= 0) {
        if (loginAbortControllerRef.current) {
          loginAbortControllerRef.current.abort();
          loginAbortControllerRef.current = null;
        }
        setEchoWaitingLogin(false);
        setEchoBrowser("your normal browser");
        setEchoCourses(auth.courses);
        const firstCourse = auth.courses[0]?.id ?? "";
        setCourseId(firstCourse);
        if (firstCourse) await loadEchoRecordings(firstCourse);
        else setEchoMessage("Signed in, but Echo360 returned no current enrolments.");
        void closeLmsTab();
        setEchoError("");
        setEchoBusy(false);
        return true;
      }
    } catch {
      // Still pending sign-in
    }
    return false;
  }, [loadEchoRecordings]);

  /**
   * Robust Echo360 connection flow with extended 5-minute login detection.
   *
   * 1. Checks if the browser is already authenticated (instant <1s path).
   * 2. If unauthenticated, opens the UniMelb Canvas LTI launch tab and starts
   *    a polite 2.5s polling loop for up to 300s, displaying an interactive progress banner.
   * 3. Once signed in, automatically closes the LMS tab and selects the first course.
   */
  const connectEcho360 = async () => {
    if (echoWaitingLogin) {
      const ok = await checkAuthNow();
      if (ok) return;
    }

    setEchoBusy(true);
    setEchoError("");
    setEchoMessage("Checking the Echo360 connector…");

    const info = await getConnectorInfo();
    const installed = info?.installed === true;
    setEchoConnectorInstalled(installed);
    if (info?.version) setEchoConnectorVersion(info.version);
    if (installed) {
      const expected = await getExpectedConnectorVersion();
      setEchoExpectedVersion(expected);
    }
    if (!installed) {
      setEchoMessage("");
      setEchoBusy(false);
      return;
    }

    // Step 1: Check existing session
    try {
      const auth = await checkEcho360Auth();
      if (auth.authenticated && auth.courses) {
        setEchoBrowser("your normal browser");
        setEchoCourses(auth.courses);
        const firstCourse = auth.courses[0]?.id ?? "";
        setCourseId(firstCourse);
        if (firstCourse) await loadEchoRecordings(firstCourse);
        else setEchoMessage("Signed in, but Echo360 returned no current enrolments.");
        setEchoBusy(false);
        return;
      }
    } catch {
      // Fall through to LMS launch if check failed
    }

    // Step 2: Open UniMelb Canvas LMS in a new tab if not already waiting
    if (echoWaitingLogin) {
      setEchoMessage("A login tab is already open. Please complete the login in that tab…");
      return;
    }
    setEchoMessage("Opening UniMelb Canvas in a new tab…");
    try {
      await openEcho360ThroughLms();
    } catch {
      // Best-effort
    }

    // Step 3: Enter background polling loop (up to 5 mins)
    if (loginAbortControllerRef.current) {
      loginAbortControllerRef.current.abort();
    }
    const controller = new AbortController();
    loginAbortControllerRef.current = controller;

    setEchoWaitingLogin(true);
    setEchoLoginSecondsLeft(300);
    setEchoMessage("Waiting for UniMelb login in the opened Canvas tab…");

    try {
      const courses = await waitForEcho360Auth({
        timeoutMs: 300_000,
        pollIntervalMs: 2_500,
        signal: controller.signal,
        onProgress: (_elapsed, remaining) => {
          setEchoLoginSecondsLeft(Math.ceil(remaining / 1000));
        },
      });

      setEchoWaitingLogin(false);
      setEchoBrowser("your normal browser");
      setEchoCourses(courses);
      const firstCourse = courses[0]?.id ?? "";
      setCourseId(firstCourse);
      if (firstCourse) await loadEchoRecordings(firstCourse);
      else setEchoMessage("Signed in, but Echo360 returned no current enrolments.");
      void closeLmsTab();
      setEchoError("");
    } catch (error) {
      if (controller.signal.aborted) return;
      setEchoWaitingLogin(false);
      setEchoError(
        error instanceof Error
          ? error.message
          : "Could not connect to Echo360.",
      );
      setEchoMessage("");
    } finally {
      loginAbortControllerRef.current = null;
      setEchoBusy(false);
    }
  };

  const importEcho360 = async () => {
    if (!courseId || !selectedRecordings.size) return;
    setEchoBusy(true);
    setEchoError("");
    setEchoMessage("Resolving selected Echo360 lectures in your browser…");
    let sources;
    try {
      sources = await resolveEcho360Sources(courseId, [...selectedRecordings], echoRecordings);
    } catch (error) {
      setEchoError(error instanceof Error ? error.message : "Could not resolve the selected Echo360 lectures.");
      setEchoMessage("");
      setEchoBusy(false);
      return;
    }
    const uploadedCount = sources.uploaded?.length || 0;
    const queuedCount = sources.sources?.length || 0;

    if (queuedCount === 0 && uploadedCount === 0) {
      setEchoError(
        "No playable media found for the selected recordings. " +
          "Check the browser console (F12) for details from the Echo360 page.",
      );
      setEchoMessage("");
      setEchoBusy(false);
      return;
    }

    let queuedTotal = uploadedCount;
    let skippedTotal = 0;

    if (queuedCount > 0) {
      setEchoMessage("Queueing remaining Echo360 lectures in the background…");
      const response = await apiFetch(apiUrl("/api/v1/lectures/echo360/import"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          course_id: courseId,
          sources: sources.sources,
          kb_name: kbName,
        }),
      });
      if (!response.ok) {
        setEchoError(await responseError(response, "Could not queue Echo360 imports."));
        setEchoMessage("");
        setEchoBusy(false);
        return;
      }
      const body = (await response.json()) as {
        queued: MediaItem[];
        skipped: number;
      };
      queuedTotal += body.queued.length;
      skippedTotal += body.skipped;
    }

    const successMsg =
      `${queuedTotal} lecture${queuedTotal === 1 ? "" : "s"} successfully imported and queued for background processing!` +
      (skippedTotal ? ` (${skippedTotal} already imported)` : "");
    setEchoSuccess(successMsg);
    setEchoMessage(successMsg);
    setSelectedRecordings(new Set());
    setEchoBusy(false);
    await refresh();
    await loadEchoRecordings(courseId);
  };

  /* ── UniMelb Canvas content import ────────────────────────────── */

  /**
   * Connect to Canvas: probes the logged-in browser session (the connector
   * opens the Canvas dashboard tab itself when no session is detected) and
   * loads the user's active courses.
   */
  const connectCanvas = useCallback(async () => {
    setCanvasBusy(true);
    setCanvasError("");
    setCanvasMessage("Checking your UniMelb Canvas session in the browser…");
    try {
      const auth = await checkCanvasAuth();
      if (auth.authenticated && auth.courses) {
        setCanvasCourses(auth.courses);
        setCanvasCourseId(auth.courses[0]?.id ?? "");
        setCanvasMessage(
          auth.courses.length
            ? "Signed in to Canvas. Pick a course, then load its content."
            : "Signed in to Canvas, but no active student enrollments were found.",
        );
        return;
      }
      setCanvasMessage(
        auth.reason
          ? `Not signed in yet (${auth.reason}). Complete the UniMelb sign-in in the opened Canvas tab, then try again.`
          : "Not signed in yet. Complete the UniMelb sign-in in the opened Canvas tab, then try again.",
      );
    } catch (error) {
      setCanvasError(
        error instanceof Error ? error.message : "Could not connect to UniMelb Canvas.",
      );
    } finally {
      setCanvasBusy(false);
    }
  }, []);

  /* Auto-connect Canvas once on mount so the course list and linked-course
   * dropdowns stay populated without a manual "Connect" button. */
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void connectCanvas();
    }, 200);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleCanvasProgress = useCallback((progress: CanvasProgress) => {
    setCanvasProgress(progress);
    setCanvasLog((prev) => {
      const line = `${new Date().toLocaleTimeString()}  ${progress.message}`;
      return [...prev.slice(-199), line];
    });
  }, []);

  /** Pull every content section for a course through the logged-in browser. */
  const loadCanvasContent = useCallback(async (nextCourseId: string) => {
    if (!nextCourseId) return;
    setCanvasBusy(true);
    setCanvasError("");
    setCanvasContent(null);
    setCanvasProgress(null);
    setCanvasLog([]);
    setCanvasMessage(
      "Reading the full Canvas course in your browser… (pages, files, assignments, quizzes)",
    );
    try {
      const result = await fetchCanvasCourseContent(nextCourseId, handleCanvasProgress);
      const content = result.content;
      setCanvasContent(content);
      const total =
        (content.pages?.length || 0) +
        (content.assignments?.length || 0) +
        (content.discussions?.length || 0) +
        (content.announcements?.length || 0) +
        (content.quizzes?.length || 0) +
        (content.files?.length || 0) +
        (content.modules?.length || 0) +
        (content.outcomes?.length || 0);
      setCanvasMessage(
        `Found ${total} content items in ${(content.course?.name as string) || "this course"}. Pick a knowledge base and import.`,
      );
    } catch (error) {
      setCanvasError(
        error instanceof Error ? error.message : "Could not read this Canvas course.",
      );
      setCanvasMessage("");
    } finally {
      setCanvasProgress(null);
      setCanvasBusy(false);
    }
  }, [handleCanvasProgress]);

  /** Render the snapshot into markdown documents and index them into the KB. */
  const importCanvasContent = async () => {
    if (!canvasCourseId || !canvasContent) return;
    setCanvasImporting(true);
    setCanvasError("");
    setCanvasMessage("Indexing Canvas content into the knowledge base…");
    try {
      const response = await apiFetch(apiUrl("/api/v1/lectures/canvas/import"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          course_id: canvasCourseId,
          course_name: canvasContent.course?.name || "",
          kb_name: kbName,
          content: canvasContent,
        }),
      });
      if (!response.ok) {
        setCanvasError(await responseError(response, "Could not import Canvas content."));
        setCanvasMessage("");
        return;
      }
      const body = (await response.json()) as { doc_count: number; kb_name: string };
      const msg = `${body.doc_count} Canvas document${body.doc_count === 1 ? "" : "s"} imported into "${body.kb_name}" for background indexing.`;
      setCanvasSuccess(msg);
      setCanvasMessage(msg);
    } catch (error) {
      setCanvasError(
        error instanceof Error ? error.message : "Could not import Canvas content.",
      );
      setCanvasMessage("");
    } finally {
      setCanvasImporting(false);
    }
  };

  /** Pull every course attachment through the user's authenticated browser
   *  and drop them into the linked KB.  The connector skips files it has
   *  already pulled (manifest-backed dedupe), so this is safe to re-run. */
  const downloadCanvasFiles = async () => {
    if (!canvasCourseId) return;
    const trimmedKb = kbName.trim();
    if (!trimmedKb) {
      setCanvasError("Pick a knowledge base before downloading files.");
      return;
    }
    setCanvasFilesBusy(true);
    setCanvasFilesResult(null);
    setCanvasError("");
    setCanvasProgress(null);
    setCanvasLog([]);
    setCanvasMessage(
      "Downloading Canvas attachments (PDFs, slides, docs) in your browser…",
    );
    try {
      const result = await fetchCanvasCourseFiles(canvasCourseId, {
        kbName: trimmedKb,
        courseName:
          (canvasContent?.course?.name as string | undefined) ??
          (canvasCourses.find((c) => c.id === canvasCourseId)?.name ?? ""),
        timeoutMs: 30 * 60 * 1000,
        onProgress: handleCanvasProgress,
      });
      setCanvasFilesResult(result);
      const summary =
        (result.uploaded_count ?? 0) +
        " uploaded, " +
        (result.failed_count ?? 0) +
        " failed of " +
        (result.total ?? 0) +
        " candidates in \"" +
        trimmedKb +
        "\".";
      setCanvasMessage(summary);
      setCanvasSuccess(summary);
    } catch (error) {
      setCanvasError(
        error instanceof Error
          ? error.message
          : "Could not download Canvas attachments.",
      );
      setCanvasMessage("");
    } finally {
      setCanvasProgress(null);
      setCanvasFilesBusy(false);
    }
  };

  /** Resolve harvested Echo360 LTI embeds to runtime media ids (walking a
   *  signed-in Canvas tab through each launch URL), download the media into
   *  the KB, and persist the resolution back to the backend manifest. */
  const resolveCanvasMedia = async () => {
    if (!canvasCourseId) return;
    const trimmedKb = kbName.trim();
    setCanvasEmbedsResolving(true);
    setCanvasEmbedsResolved([]);
    setCanvasError("");
    setCanvasProgress(null);
    setCanvasLog([]);
    setCanvasMessage("Looking up Echo360 media links from the Canvas course…");
    try {
      const listRes = await apiFetch(
        apiUrl(`/api/v1/lectures/echo360/embeds/${encodeURIComponent(canvasCourseId)}`),
      );
      if (!listRes.ok) {
        setCanvasError(await responseError(listRes, "Could not list Echo360 embeds."));
        setCanvasMessage("");
        return;
      }
      const listBody = (await listRes.json()) as { embeds: any[] };
      const embeds = (listBody.embeds || [])
        .filter((e) => !e.media_id) // only re-resolve unresolved links
        .map((e) => ({
          uuid: String(e.uuid || ""),
          title: String(e.title || ""),
          canvas_launch_url: String(e.canvas_launch_url || ""),
        }))
        .filter((e) => e.uuid && e.canvas_launch_url);
      if (!embeds.length) {
        setCanvasMessage("Every Echo360 media link in this course is already resolved.");
        return;
      }
      setCanvasMessage(`Resolving ${embeds.length} Echo360 media link(s) in your browser…`);
      const courseName =
        (canvasContent?.course?.name as string | undefined) ??
        canvasCourses.find((c) => c.id === canvasCourseId)?.name ?? "";
      const result = await resolveCanvasEmbeds(
        embeds,
        { download: true, kbName: trimmedKb, courseId: canvasCourseId, courseName },
        handleCanvasProgress,
      );
      const resolved = result.resolved || [];
      setCanvasEmbedsResolved(resolved);

      const saveRes = await apiFetch(
        apiUrl("/api/v1/lectures/echo360/embeds/resolve"),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ course_id: canvasCourseId, resolved }),
        },
      );
      if (!saveRes.ok) {
        setCanvasError(await responseError(saveRes, "Could not save resolved media links."));
      }

      const okCount = resolved.filter((r) => r.media_id).length;
      const dls = result.downloads;
      const downloaded =
        (dls?.uploaded.length ?? 0) + (dls?.queued.length ?? 0) + (dls?.skipped ?? 0);
      const dlFailed = dls?.failed.length ?? 0;
      const summary = dls
        ? `Resolved ${okCount}/${resolved.length} media link(s); downloaded/queued ${downloaded}, failed ${dlFailed}.`
        : `Resolved ${okCount}/${resolved.length} Echo360 media link(s) to media ids.`;
      setCanvasMessage(summary);
      setCanvasSuccess(summary);
    } catch (error) {
      setCanvasError(
        error instanceof Error ? error.message : "Could not resolve Echo360 media links.",
      );
      setCanvasMessage("");
    } finally {
      setCanvasProgress(null);
      setCanvasEmbedsResolving(false);
    }
  };

  const toggleRecording = (recordingId: string) => {
    setSelectedRecordings((current) => {
      const next = new Set(current);
      if (next.has(recordingId)) next.delete(recordingId);
      else next.add(recordingId);
      return next;
    });
  };

  // Plex: defer search query to avoid O(n) .toLowerCase().includes() per keystroke
  const deferredQuery = useDeferredValue(query);

  const visible = useMemo(
    () =>
      items.filter((item) =>
        `${item.title} ${item.summary ?? ""} ${item.course_name ?? ""} ${item.series_name ?? ""}`
          .toLowerCase()
          .includes(deferredQuery.toLowerCase()),
      ),
    [items, deferredQuery],
  );

  /** Netflix-style grouping: each course becomes a "series" whose episodes are
   *  ordered chronologically by their recorded date. */
  const series = useMemo(() => {
    const groups = new Map<string, MediaItem[]>();
    for (const item of visible) {
      const name = item.series_name || item.course_name || "Library";
      const list = groups.get(name) ?? [];
      list.push(item);
      groups.set(name, list);
    }
    return [...groups.entries()]
      .map(([name, episodes]) => ({
        name,
        episodes: episodes.sort((a, b) =>
          (a.series_date || a.created_at || "").localeCompare(
            b.series_date || b.created_at || "",
          ),
        ),
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [visible]);

  // ── Resume-watching ─────────────────────────────────────────────────────
  const lastProgressSaveRef = useRef(0);
  const pendingSeekRef = useRef<number | null>(null);

  const saveProgress = useCallback(
    async (itemId: string, position: number, duration?: number) => {
      try {
        await apiFetch(apiUrl(`/api/v1/lectures/${itemId}/progress`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            position_seconds: position,
            duration_seconds: duration ?? null,
          }),
        });
      } catch {
        // Best-effort checkpoint; ignore network hiccups.
      }
    },
    [],
  );

  const handleTimeUpdate = useCallback(
    (item: MediaItem, position: number, duration?: number) => {
      setAudioCurrentTime(position);
      const now = Date.now();
      if (now - lastProgressSaveRef.current > 5000) {
        lastProgressSaveRef.current = now;
        // Completed → checkpoint at 0 so the next open starts from the top.
        const effective = duration && position >= duration - 3 ? 0 : position;
        void saveProgress(item.id, effective, duration);
      }
    },
    [saveProgress],
  );

  const openItem = useCallback((item: MediaItem) => {
    const sName = item.series_name || item.course_name || "Library";
    setActiveSeries(sName);
    const pos = item.position_seconds || 0;
    const dur = item.duration_seconds || 0;
    // Resume only when there's meaningful progress left to watch.
    pendingSeekRef.current = pos > 5 && (!dur || pos < dur - 10) ? pos : null;
    lastProgressSaveRef.current = 0;
    setActive(item);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const featuredItem = !active && visible.length > 0 
    ? visible.find(item => {
        const pct = item.duration_seconds && item.duration_seconds > 0 && item.position_seconds ? item.position_seconds / item.duration_seconds : 0;
        return pct > 0.01 && pct < 0.97;
      }) || visible[0]
    : null;

  return (
    <main className="bg-[#0f0f10] min-h-screen h-full overflow-y-auto text-zinc-200 selection:bg-cyan-500/30 font-sans pb-24 relative">
      <style dangerouslySetInnerHTML={{ __html: `
        .beach-surface { background-color: #0f0f10 !important; }
        .ocean-sidebar-root { display: none !important; }
        .vds-captions, [data-part="captions"], .vds-caption-renderer { display: none !important; }
      `}} />
      
      {/* Sleek Top Nav */}
      <nav className="sticky top-0 z-50 flex items-center justify-between px-6 py-4 bg-gradient-to-b from-[#0f0f10] via-[#0f0f10]/80 to-transparent backdrop-blur-sm">
        <div className="flex items-center gap-8">
          <div className="flex items-center gap-2 text-cyan-500 font-black tracking-widest uppercase text-lg drop-shadow-md">
            <Waves size={24} strokeWidth={3} />
            <span>Nuvio</span>
          </div>
          <div className="hidden md:flex items-center gap-8 text-sm font-semibold text-zinc-400">
             <Link href="/home" className="hover:text-white transition-colors">Home</Link>
             <Link href="/library" className="text-white drop-shadow-sm">Library</Link>
             <a href="#media-settings" className="hover:text-white transition-colors">Settings</a>
          </div>
        </div>
        <div className="flex items-center gap-4">
           {/* Search Bar */}
           <div className="hidden lg:flex items-center gap-2 rounded-full bg-white/5 border border-white/10 px-4 py-1.5 focus-within:bg-white/10 transition-colors w-64">
             <Search size={14} className="text-zinc-400 shrink-0" />
             <input
               value={query}
               onChange={(event) => setQuery(event.target.value)}
               placeholder="Search..."
               className="bg-transparent text-sm text-white outline-none placeholder:text-zinc-500 w-full"
             />
           </div>
           
           {/* Upload/Sync buttons */}
           <button onClick={() => void runSyncNow()} disabled={syncing} className="text-zinc-400 hover:text-white transition-colors p-2" title="Sync">
              <RefreshCw size={18} className={syncing ? "animate-spin" : ""} />
           </button>
           <button onClick={() => input.current?.click()} disabled={uploading} className="rounded-full bg-white px-5 py-2 text-xs font-bold text-black hover:bg-zinc-200 transition-colors shadow-lg shadow-white/10 flex items-center gap-2">
              <Plus size={14} strokeWidth={3} /> Add Media
           </button>
        </div>
      </nav>
      <input ref={input} type="file" className="hidden" accept="audio/*,video/mp4,video/webm,video/quicktime" onChange={(event) => void upload(event.target.files?.[0])} />

      {/* Hero Banner */}
      {!active && featuredItem && (
        <div className="relative w-full h-[70vh] min-h-[500px] mb-8 -mt-20">
          <div className="absolute inset-0 z-0">
            {featuredItem.content_type.startsWith("video/") ? (
              <video
                src={apiUrl(`/api/v1/lectures/${featuredItem.id}/media#t=300.0`)}
                preload="metadata"
                className="w-full h-full object-cover opacity-80"
                muted
                playsInline
              />
            ) : (
               <div className="w-full h-full bg-zinc-900" />
            )}
            <div className="absolute inset-0 bg-gradient-to-r from-[#0f0f10] via-[#0f0f10]/80 to-transparent" />
            <div className="absolute inset-0 bg-gradient-to-t from-[#0f0f10] via-[#0f0f10]/20 to-transparent" />
          </div>
          
          <div className="relative z-10 flex h-full flex-col justify-end px-8 pb-16 md:px-16 md:w-2/3">
             <span className="text-cyan-400 font-black uppercase tracking-widest text-xs mb-3 drop-shadow-lg flex items-center gap-2">
               {(() => {
                 const pct = featuredItem.duration_seconds && featuredItem.duration_seconds > 0 && featuredItem.position_seconds ? featuredItem.position_seconds / featuredItem.duration_seconds : 0;
                 const isResume = pct > 0.01 && pct < 0.97;
                 return isResume ? (
                   <>
                     <span className="text-emerald-400">Continue Watching</span>
                     <span className="text-cyan-400/50">•</span>
                     {featuredItem.course_name || "Featured"}
                   </>
                 ) : (
                   featuredItem.course_name || "Featured"
                 );
               })()}
             </span>
             <h1 className="text-5xl md:text-7xl font-black text-white leading-tight mb-4 drop-shadow-2xl filter" style={{ textShadow: '0 4px 20px rgba(0,0,0,0.8)' }}>
               {featuredItem.title}
             </h1>
             <p className="text-zinc-300 text-sm md:text-base mb-8 line-clamp-3 max-w-xl drop-shadow-lg font-medium">
               {featuredItem.summary || "No description available for this lecture. Watch now to discover its contents."}
             </p>
             <div className="flex items-center gap-4">
                <button onClick={() => featuredItem.content_type.startsWith("video/") ? router.push(`/library/${featuredItem.id}`) : openItem(featuredItem)} className="flex items-center gap-2 rounded-full bg-cyan-600 px-8 py-3.5 text-white font-bold hover:bg-cyan-500 transition-colors shadow-xl shadow-cyan-900/40 hover:scale-105 active:scale-95 duration-200">
                 <Play size={20} fill="currentColor" /> 
                 {(() => {
                   const pct = featuredItem.duration_seconds && featuredItem.duration_seconds > 0 && featuredItem.position_seconds ? featuredItem.position_seconds / featuredItem.duration_seconds : 0;
                   return (pct > 0.01 && pct < 0.97) ? "Resume" : "Play Now";
                 })()}
               </button>
               <button className="flex items-center gap-2 rounded-full bg-zinc-800/80 px-8 py-3.5 text-white font-bold backdrop-blur-md border border-white/10 hover:bg-zinc-700 hover:scale-105 active:scale-95 transition-all duration-200">
                 More Info
               </button>
             </div>
          </div>
        </div>
      )}

      <section className="mx-auto max-w-[1400px] px-6 md:px-12 relative z-10">
        {active && (
          <div className="mb-12 mt-4 grid gap-8 rounded-3xl border border-white/10 bg-black/40 backdrop-blur-2xl p-6 shadow-2xl lg:grid-cols-[minmax(0,1.5fr)_minmax(340px,1fr)]">
            <div className="overflow-hidden rounded-xl bg-black">
              {active.content_type.startsWith("video/") ? (
                <div className="overflow-hidden rounded-xl bg-black">
                  <div className="relative aspect-video w-full overflow-hidden rounded-xl bg-black">
                    <MediaPlayer
                      ref={playerRef}
                      key={active.id}
                      src={{
                        src: apiUrl(`/api/v1/lectures/${active.id}/media`),
                        type: (active.content_type || "video/mp4") as "video/mp4",
                      }}
                      title={active.title}
                      autoPlay
                      playsInline
                      className="aspect-video w-full"
                      onTimeUpdate={(e) =>
                        handleTimeUpdate(
                          active,
                          e.currentTime,
                          playerRef.current?.duration,
                        )
                      }
                      onCanPlay={() => {
                        const target = pendingSeekRef.current;
                        if (target != null && playerRef.current) {
                          playerRef.current.currentTime = target;
                          pendingSeekRef.current = null;
                        }
                      }}
                    >
                      <MediaProvider>
                        <Track
                          kind="captions"
                          src={apiUrl(`/api/v1/lectures/${active.id}/subtitles?mode=karaoke`)}
                          lang="en"
                          label="Word-by-Word Karaoke"
                          default
                        />
                        <Track
                          kind="captions"
                          src={apiUrl(`/api/v1/lectures/${active.id}/subtitles?mode=standard`)}
                          lang="en-US"
                          label="Standard Subtitles (Line-by-Line)"
                        />
                      </MediaProvider>
                      {/* Plex fix: slots={{}} ensures DefaultVideoLayout renders all controls (play, volume, time, etc).
                          Previously `slots={{ captions: null }}` was intended to hide only the native captions
                          button but in some Vidstack versions it suppressed the entire control bar.  We keep
                          our custom KaraokeSubtitleOverlay for word-by-word display; the Track elements inside
                          MediaProvider provide the textTrack state that powers it. */}
                      <DefaultVideoLayout
                        thumbnails={apiUrl(`/api/v1/lectures/${active.id}/thumbnails.vtt`)}
                        slots={{}}
                        icons={defaultLayoutIcons}
                      />
                      <KaraokeSubtitleOverlay
                        segments={activeSegments}
                        onSeek={(time) => {
                          if (playerRef.current) {
                            playerRef.current.currentTime = time;
                          }
                        }}
                      />
                      <IntroSkipOverlay
                        introDuration={active.intro_duration || (active.source === "echo360" ? 14.0 : 0)}
                        autoSkip={autoSkipIntro}
                        onSkip={() => {
                          if (playerRef.current) {
                            playerRef.current.currentTime = active.intro_duration || 14.0;
                          }
                        }}
                      />
                    </MediaPlayer>
                  </div>
                </div>
              ) : (
                <div className="flex min-h-48 flex-col items-center justify-center p-8">
                  <audio
                    controls
                    autoPlay
                    className="w-full"
                    src={apiUrl(`/api/v1/lectures/${active.id}/media`)}
                    onTimeUpdate={(e) =>
                      handleTimeUpdate(active, e.currentTarget.currentTime, e.currentTarget.duration)
                    }
                    onLoadedMetadata={(e) => {
                      const target = pendingSeekRef.current;
                      if (target != null) {
                        e.currentTarget.currentTime = target;
                        pendingSeekRef.current = null;
                      }
                    }}
                  />

                  {/* Audio Subtitle Mode Switcher */}
                  <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
                    <button
                      type="button"
                      onClick={() =>
                        setAudioKaraokeMode(
                          audioKaraokeMode === "karaoke"
                            ? "standard"
                            : audioKaraokeMode === "standard"
                            ? "off"
                            : "karaoke",
                        )
                      }
                      className={`inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium transition ${
                        audioKaraokeMode === "karaoke"
                          ? "bg-cyan-600 text-white"
                          : audioKaraokeMode === "standard"
                          ? "bg-slate-700 text-slate-200"
                          : "border border-white/10 bg-white/5 text-zinc-400"
                      }`}
                    >
                      {audioKaraokeMode === "karaoke"
                        ? "🎤 Karaoke (Word-by-Word)"
                        : audioKaraokeMode === "standard"
                        ? "📄 Standard Subtitles"
                        : "❌ Subtitles Off"}
                    </button>
                  </div>

                  {audioKaraokeMode !== "off" && (() => {
                    const seg = activeSegments.find(
                      (s) =>
                        audioCurrentTime >= s.start - 0.05 &&
                        audioCurrentTime <= s.end + 0.15,
                    );
                    if (!seg) return null;
                    return (
                      <div className="mt-5 flex justify-center text-center">
                        <div className="inline-block max-w-[92%] rounded-2xl border border-white/15 bg-[#090d16]/92 px-5 py-3 text-base font-bold text-white shadow-2xl backdrop-blur-md">
                          {audioKaraokeMode === "karaoke" ? (
                            seg.words.map((w, idx) => {
                              const isActive =
                                audioCurrentTime >= w.start &&
                                audioCurrentTime <= w.end;
                              const isPassed = audioCurrentTime > w.end;
                              return (
                                <span
                                  key={idx}
                                  className={`mx-1 inline-block transition-colors duration-100 ${
                                    isActive
                                      ? "text-cyan-400 font-semibold"
                                      : isPassed
                                      ? "text-white"
                                      : "text-zinc-400/60"
                                  }`}
                                >
                                  {w.text}
                                </span>
                              );
                            })
                          ) : (
                            <span className="text-white">{seg.text}</span>
                          )}
                        </div>
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>

            {/* Right Sidepane: Live Subtitles / Overview */}
            <div className="flex flex-col min-h-[380px] max-h-[560px]">
              {/* Tab Switcher & Intro Auto-skip */}
              <div className="mb-3 flex items-center justify-between gap-2 border-b border-white/10 pb-2.5">
                <div className="flex items-center gap-1.5 rounded-lg bg-white/5 border border-white/10 p-0.5 text-xs font-medium"> {/* Nuvio dark fix — tab switcher */}
                  <button
                    type="button"
                    onClick={() => setSidepaneTab("transcript")}
                    className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 transition cursor-pointer ${
                      sidepaneTab === "transcript"
                        ? "bg-white/5 text-cyan-500 shadow-sm font-semibold"
                        : "text-zinc-400 hover:text-white"
                    }`}
                  >
                    <span>Live Subtitles</span>
                    <span className="rounded bg-cyan-500/15 px-1 py-0.2 text-[10px] font-bold text-cyan-500">
                      {activeSegments.length}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setSidepaneTab("overview")}
                    className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 transition cursor-pointer ${
                      sidepaneTab === "overview"
                        ? "bg-white/5 text-white shadow-sm font-semibold"
                        : "text-zinc-400 hover:text-white"
                    }`}
                  >
                    <span>Overview & Notes</span>
                  </button>
                </div>

                {active.content_type.startsWith("video/") && (
                  <button
                    type="button"
                    onClick={() => {
                      const next = !autoSkipIntro;
                      setAutoSkipIntro(next);
                      if (typeof window !== "undefined") {
                        localStorage.setItem("unisync_auto_skip_intro", next ? "true" : "false");
                      }
                    }}
                    className={`inline-flex h-6 items-center gap-1 rounded-full border px-2 text-[10px] font-medium transition cursor-pointer ${
                      autoSkipIntro
                        ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-400"
                        : "border-white/10 bg-white/5 text-zinc-400" // review fix
                    }`}
                    title="Automatically skip the 14s copyright disclaimer at the start of recordings"
                  >
                    <FastForward size={10} className={autoSkipIntro ? "text-cyan-400" : "text-zinc-400"} />
                    <span>Auto-skip</span>
                  </button>
                )}
              </div>

              {sidepaneTab === "transcript" ? (
                <TranscriptSidepane
                  segments={activeSegments}
                  currentTime={audioCurrentTime}
                  onSeek={(time) => {
                    if (active.content_type.startsWith("video/") && playerRef.current) {
                      playerRef.current.currentTime = time;
                    } else {
                      setAudioCurrentTime(time);
                      const audioEl = document.querySelector("audio");
                      if (audioEl) audioEl.currentTime = time;
                    }
                  }}
                />
              ) : (
                <div className="overflow-y-auto pr-1">
                  <h2 className="text-lg font-semibold">{active.title}</h2>
                  {active.course_name && (
                    <p className="mt-1 text-xs text-zinc-400">
                      {active.course_name}
                    </p>
                  )}
                  <p className="mt-3 whitespace-pre-line text-sm leading-6 text-zinc-400">
                    {active.summary || active.progress}
                  </p>
                  <div className="mt-4 flex gap-3 text-xs">
                    <a
                      className="text-[var(--primary)] underline"
                      href={apiUrl(`/api/v1/lectures/${active.id}/notes`)}
                      target="_blank"
                    >
                      Study notes
                    </a>
                    <a
                      className="text-[var(--primary)] underline"
                      href={apiUrl(`/api/v1/lectures/${active.id}/transcript`)}
                      target="_blank"
                    >
                      Raw Transcript
                    </a>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

                {!activeSeries ? (
          <div className="mb-12">
            <h2 className="text-2xl font-bold mb-6 text-white px-1">Your Courses & Series</h2>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6">
              {series.map((s) => {
                const lib = libraries.find(l => l.id.toLowerCase() === s.name.toLowerCase() || l.name.toLowerCase() === s.name.toLowerCase());
                const canvasCourse = lib ? canvasCourses.find(c => c.id === lib.canvas_course_id) : null;
                // Plex: fallback to stored thumbnail URL when Canvas connector isn't live
                const posterUrl = canvasCourse?.image_download_url || lib?.image_download_url || null;
                // Plex: warn when no thumbnail available for debugging sync issues
                if (!posterUrl) {
                  console.warn(`[library] No thumbnail for "${s.name}" — lib.canvas_course_id=${lib?.canvas_course_id || "(none)"}, image_download_url=${lib?.image_download_url || "(missing)"}`);
                }

                return (
                  <div 
                    key={s.name} 
                    onClick={() => {
                        setActiveSeries(s.name);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                    className="group relative aspect-[2/3] w-full cursor-pointer overflow-hidden rounded-xl bg-zinc-900 shadow-sm transition-all duration-500 hover:scale-[1.03] hover:shadow-2xl hover:shadow-cyan-900/20 ring-1 ring-white/10 hover:ring-white/30"
                  >
                    {/* Series Poster (Canvas Image) */}
                    <div className="absolute inset-0 z-0 bg-black/80">
                      {posterUrl ? (
                        <img
                          src={posterUrl}
                          className="w-full h-full object-cover opacity-60 transition-transform duration-700 group-hover:scale-110 group-hover:opacity-100"
                          alt={s.name}
                          loading="lazy"
                          decoding="async"
                        />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center bg-gradient-to-br from-zinc-800 to-[#0f0f10] text-zinc-600 transition-transform duration-700 group-hover:scale-110">
                          <Layers size={48} />
                        </div>
                      )}
                      <div className="absolute inset-0 bg-gradient-to-t from-[#0f0f10] via-black/40 to-transparent" />
                    </div>
                    
                    {/* Series Info */}
                    <div className="absolute inset-x-0 bottom-0 z-10 flex flex-col p-4">
                      <h3 className="text-lg font-bold leading-tight text-white drop-shadow-md line-clamp-2 mb-1 group-hover:text-cyan-400 transition-colors">
                        {s.name}
                      </h3>
                      <span className="text-xs font-semibold text-zinc-400 drop-shadow-sm">
                        {s.episodes.length} Episode{s.episodes.length === 1 ? "" : "s"}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <div className="space-y-8 mb-12">
            <div className="flex items-center justify-between mb-4 px-1">
               <div>
                 <button 
                   onClick={() => {
                     setActiveSeries(null);
                     setActive(null);
                   }}
                   className="flex items-center gap-2 text-sm font-semibold text-cyan-500 hover:text-cyan-400 mb-2 transition-colors"
                 >
                   <ArrowDown className="rotate-90" size={16} /> Back to Series
                 </button>
                 <h2 className="text-3xl font-black text-white drop-shadow-md">{activeSeries}</h2>
                 <span className="text-sm text-zinc-400">
                    {series.find(s => s.name === activeSeries)?.episodes.length || 0} Episodes
                 </span>
               </div>
            </div>
            
            {/* Horizontal Swimlanes for Episodes */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
              {series.find(s => s.name === activeSeries)?.episodes.map((item, index) => {
                const Icon = item.content_type.startsWith("video/") ? FileVideo : FileAudio;
                const pct = item.duration_seconds
                  ? Math.min((item.position_seconds || 0) / item.duration_seconds, 1)
                  : 0;
                const resume = pct > 0.01 && pct < 0.97;
                return (
                  <div
                    key={item.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => item.content_type.startsWith("video/") ? router.push(`/library/${item.id}`) : openItem(item)}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") item.content_type.startsWith("video/") ? router.push(`/library/${item.id}`) : openItem(item); }}
                    className="group flex flex-col w-full shrink-0 cursor-pointer text-left transition-all duration-300"
                  >
                    {/* Landscape Poster */}
                    <div className="relative w-full aspect-video overflow-hidden rounded-xl bg-zinc-900 shadow-sm transition-all duration-300 group-hover:shadow-xl group-hover:scale-[1.03] border border-white/10 group-hover:border-white/20 ring-1 ring-black/5">
                      {/* Content Based Thumbnail */}
                      <div className="absolute inset-0 z-0 overflow-hidden bg-black/80">
                        {item.content_type.startsWith("video/") ? (
                          <LazyVideoThumb
                            src={apiUrl(`/api/v1/lectures/${item.id}/media#t=120.0`)}
                            className="w-full h-full"
                          />
                        ) : (
                          <div className="flex h-full w-full items-center justify-center bg-black/80 text-slate-400 transition-transform duration-500 group-hover:scale-110">
                            <Icon size={48} />
                          </div>
                        )}
                        {/* Inner gradients for depth */}
                        <div className="absolute inset-0 bg-gradient-to-t from-[#0f0f10] via-transparent to-black/30" />
                      </div>
                      
                      {/* Top Badges */}
                      <div className="absolute top-2 inset-x-2 z-10 flex items-start justify-between">
                        <div className="flex flex-col gap-1.5">
                          <div className="w-min rounded bg-black/50 px-1.5 py-1 backdrop-blur-md shadow-sm">
                            <Icon size={14} className="text-white/90" />
                          </div>
                          {item.source === "echo360" && (
                            <span className="w-min rounded bg-cyan-600/90 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-white backdrop-blur-sm shadow-sm">
                              Echo
                            </span>
                          )}
                        </div>
                        
                        <button
                          onClick={(e) => { e.stopPropagation(); void deleteMediaItem(item.id, e); }}
                          className="rounded-full bg-black/50 p-1.5 text-white/80 opacity-0 transition-all duration-200 group-hover:opacity-100 hover:bg-red-600 hover:text-white hover:scale-110 backdrop-blur-md shadow-sm"
                          title="Delete lecture item"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                      
                      {/* Play overlay on hover */}
                      <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/30 opacity-0 backdrop-blur-[2px] transition-opacity duration-300 group-hover:opacity-100">
                        <div className="rounded-full bg-white/10 p-4 backdrop-blur-md transition-transform hover:scale-110 shadow-lg border border-white/20">
                          <Play size={28} fill="currentColor" className="text-white translate-x-0.5 drop-shadow-md" />
                        </div>
                      </div>

                      {/* Bottom-left Badges */}
                      <div className="absolute bottom-3 left-2 z-10 flex items-center gap-1.5">
                         <span className="rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-widest text-white/95 backdrop-blur-md shadow-sm">
                           Ep {index + 1}
                         </span>
                         {item.status === "ready" && (
                           <span className="rounded bg-emerald-600/90 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-white backdrop-blur-md shadow-sm">
                             HD
                           </span>
                         )}
                      </div>
                      
                      {resume && (
                        <span className="absolute inset-x-0 bottom-0 z-10 h-1.5 bg-black/40">
                          <span
                            className="block h-full bg-cyan-500 rounded-r-sm"
                            style={{ width: `${Math.round(pct * 100)}%` }}
                          />
                        </span>
                      )}
                    </div>
                    
                    {/* Text Info Below Poster */}
                    <div className="mt-3 flex flex-col px-1">
                      <h3 className="line-clamp-2 text-sm font-semibold leading-snug text-white group-hover:text-cyan-400 transition-colors drop-shadow-sm">
                        {item.title}
                      </h3>
                      <div className="mt-1 flex items-center gap-1.5 text-xs text-zinc-400 font-medium">
                        {item.series_date && (
                           <>
                             <span>{new Date(item.series_date).toLocaleDateString()}</span>
                           </>
                        )}
                      </div>
                      
                      {/* Status line */}
                      <div className="mt-2 flex items-center gap-1.5">
                        {item.status === "downloaded" && (
                          <button
                            onClick={(e) => { e.stopPropagation(); void reprocessMediaItem(item.id, e); }}
                            className="rounded px-2 py-0.5 text-[10px] font-bold bg-white/10 text-white hover:bg-cyan-600 transition-colors border border-white/5"
                            title="Retry Speech-to-Text Transcription"
                          >
                            Retry
                          </button>
                        )}
                        <span
                          className={`rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ${
                            item.status === "ready"
                              ? "bg-emerald-500/20 text-emerald-400"
                              : item.status === "downloaded"
                                ? "bg-sky-500/20 text-sky-400"
                                : item.status === "failed"
                                  ? "bg-red-500/20 text-red-400"
                                  : "bg-amber-500/20 text-amber-400 animate-pulse"
                          }`}
                          title={item.progress}
                        >
                          {item.status}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
        
        {!visible.length && (
          <div className="rounded-2xl border border-dashed border-slate-200 py-20 text-center text-sm text-slate-600">
            Your library is waiting for its first lecture.
          </div>
        )}
      
        <div className="my-16 h-px w-full bg-slate-200" />
        
        <div className="my-16 h-px w-full bg-white/5" />
        
        <div id="media-settings" className="opacity-50 hover:opacity-100 transition-opacity duration-500 pt-8">
          <h2 className="text-xl font-semibold text-white/90 mb-6 px-1">Sources & Settings</h2>
{/* ── Course Libraries Section ── */}
        <div className="mb-6 overflow-hidden rounded-2xl border border-white/10 bg-black/60 p-6 shadow-2xl backdrop-blur-xl relative">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between z-10 relative">
            <div className="flex items-center gap-4">
              <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white/5 text-white/80 border border-white/10 shadow-inner">
                <BookOpen size={24} />
              </span>
              <div>
                <h2 className="text-xl font-bold tracking-tight text-white drop-shadow-md">Course Libraries</h2>
                <p className="mt-1.5 max-w-2xl text-sm font-medium leading-relaxed text-zinc-300 drop-shadow-sm">
                  Link course libraries (e.g. COMP10001) to specific Knowledge Bases & Echo360 courses for automatic background ingestion.
                </p>
              </div>
            </div>
            <button
              onClick={openCreateLibraryModal}
              className="inline-flex h-9 items-center justify-center gap-1.5 rounded-xl bg-[var(--primary)] px-3.5 text-xs font-medium text-[var(--primary-foreground)] shadow-sm hover:opacity-90 transition-opacity"
            >
              <Plus size={15} />
              Add Course Library
            </button>
          </div>

          {libraries.length === 0 ? (
            <div className="mt-4 rounded-xl border border-dashed border-white/10 p-6 text-center text-xs text-zinc-400">
              No course libraries set up yet. Click <strong>Add Course Library</strong> to create one (e.g., COMP10001) and link it to your Echo360 course!
            </div>
          ) : (
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {libraries.map((lib) => (
                <div key={lib.id} className="relative flex flex-col justify-between rounded-xl border border-white/10 bg-transparent p-4 shadow-sm transition-all hover:border-[var(--primary)]/40">
                  <div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="rounded-md bg-[var(--primary)]/15 px-2 py-0.5 text-xs font-bold text-[var(--primary)]">
                        {lib.id.toUpperCase()}
                      </span>
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => openEditLibraryModal(lib)}
                          className="rounded-lg p-1.5 text-zinc-400 hover:bg-[var(--accent)] hover:text-white transition-colors"
                          title="Edit Library"
                        >
                          <Edit3 size={14} />
                        </button>
                        <button
                          onClick={() => void deleteCourseLibrary(lib.id)}
                          className="rounded-lg p-1.5 text-zinc-400 hover:bg-red-500/10 hover:text-red-600 transition-colors"
                          title="Delete Library"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                    <h3 className="mt-2 font-medium text-sm text-white truncate">{lib.name}</h3>
                    
                    <div className="mt-3 space-y-1 text-xs text-zinc-400">
                      <div className="flex items-center justify-between">
                        <span>Knowledge Base:</span>
                        <span className="font-mono text-white">{lib.kb_name}</span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span>Echo360 Course:</span>
                        <span className="truncate max-w-[140px] font-mono text-white">
                          {echoCourses.find((c) => c.id === lib.echo360_course_id)?.name || lib.echo360_course_id || "Not linked"}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span>Canvas Course:</span>
                        <span className="truncate max-w-[140px] font-mono text-white">
                          {canvasCourses.find((c) => c.id === lib.canvas_course_id)?.name || lib.canvas_course_id || "Not linked"}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="mt-4 flex items-center justify-between border-t border-white/10 pt-2.5 text-[11px]">
                    <span className="flex items-center gap-1.5">
                      {lib.auto_sync ? (
                        <>
                          <span className="relative flex h-2 w-2">
                            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"></span>
                            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500"></span>
                          </span>
                          <span className="text-emerald-600 font-medium dark:text-emerald-400">Auto-Sync Active</span>
                        </>
                      ) : (
                        <span className="text-zinc-400">Manual Sync Only</span>
                      )}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── Create / Edit Library Modal ── */}
        {showLibModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
            <div className="w-full max-w-md rounded-2xl border border-white/10 bg-white/5 p-6 shadow-xl">
              <div className="flex items-center justify-between pb-3 border-b border-white/10">
                <h3 className="text-base font-semibold text-white">
                  {editingLib ? "Edit Course Library" : "Create Course Library"}
                </h3>
                <button
                  onClick={() => setShowLibModal(false)}
                  className="rounded-lg p-1 text-zinc-400 hover:bg-[var(--accent)]"
                >
                  ✕
                </button>
              </div>

              <form onSubmit={saveCourseLibrary} className="mt-4 space-y-4 text-xs sm:text-sm">
                <div>
                  <label className="block mb-1 font-medium text-white">
                    Library Code / Slug <span className="text-red-500">*</span>
                  </label>
                  <input
                    disabled={!!editingLib}
                    value={libId}
                    onChange={(e) => setLibId(e.target.value)}
                    placeholder="e.g. comp10001"
                    className="w-full h-10 rounded-xl border border-white/10 bg-transparent px-3 outline-none disabled:opacity-50"
                    required
                  />
                  <p className="mt-1 text-[11px] text-zinc-400">Unique code for this course (e.g. comp10001).</p>
                </div>

                <div>
                  <label className="block mb-1 font-medium text-white">
                    Display Title <span className="text-red-500">*</span>
                  </label>
                  <input
                    value={libName}
                    onChange={(e) => setLibName(e.target.value)}
                    placeholder="e.g. COMP10001 Foundations of Computing"
                    className="w-full h-10 rounded-xl border border-white/10 bg-transparent px-3 outline-none"
                    required
                  />
                </div>

                <div>
                  <label className="block mb-1 font-medium text-white">
                    Linked Echo360 Course
                  </label>
                  <select
                    value={libCourseId}
                    onChange={(e) => setLibCourseId(e.target.value)}
                    className="w-full h-10 rounded-xl border border-white/10 bg-transparent px-3 outline-none text-xs"
                  >
                    <option value="">-- Select Echo360 Course (Optional) --</option>
                    {echoCourses.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block mb-1 font-medium text-white">
                    Linked Canvas Course
                  </label>
                  <select
                    value={libCanvasCourseId}
                    onChange={(e) => setLibCanvasCourseId(e.target.value)}
                    className="w-full h-10 rounded-xl border border-white/10 bg-transparent px-3 outline-none text-xs"
                  >
                    <option value="">-- Select Canvas Course (Optional) --</option>
                    {canvasCourses.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                  <p className="mt-1 text-[11px] text-zinc-400">
                    Canvas content (pages, files, assignments…) imports into this Knowledge Base and auto-syncs daily.
                  </p>
                </div>

                <div>
                  <label className="block mb-1 font-medium text-white">
                    Bound Knowledge Base
                  </label>
                  <input
                    value={libKbName}
                    onChange={(e) => setLibKbName(e.target.value)}
                    placeholder={libId ? `${libId.toLowerCase()}_kb` : "e.g. comp10001_kb"}
                    className="w-full h-10 rounded-xl border border-white/10 bg-transparent px-3 outline-none"
                  />
                  <p className="mt-1 text-[11px] text-zinc-400">
                    Target Knowledge Base where lecture notes will be indexed. Auto-created if missing.
                  </p>
                </div>

                <div className="flex items-center gap-3 pt-2">
                  <input
                    type="checkbox"
                    id="libAutoSyncToggle"
                    checked={libAutoSync}
                    onChange={(e) => setLibAutoSync(e.target.checked)}
                    className="h-4 w-4 rounded border-white/10 text-[var(--primary)] cursor-pointer"
                  />
                  <label htmlFor="libAutoSyncToggle" className="cursor-pointer text-xs font-medium">
                    Automatically download & ingest new Echo360 uploads and Canvas content into this Knowledge Base
                  </label>
                </div>

                <div className="flex items-center justify-end gap-2 pt-4 border-t border-white/10">
                  <button
                    type="button"
                    onClick={() => setShowLibModal(false)}
                    className="h-9 px-4 rounded-xl border border-white/10 text-xs font-medium text-zinc-400 hover:bg-[var(--accent)]"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={libSaving}
                    className="inline-flex h-9 items-center justify-center gap-1.5 rounded-xl bg-[var(--primary)] px-4 text-xs font-medium text-[var(--primary-foreground)] disabled:opacity-50 shadow-sm"
                  >
                    {libSaving && <Loader2 size={14} className="animate-spin" />}
                    {editingLib ? "Save Changes" : "Create Library"}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        <div className="mb-6 overflow-hidden rounded-2xl border border-cyan-500/20 bg-black/60 p-6 shadow-2xl backdrop-blur-xl relative">
          <div className="absolute top-0 right-0 p-32 bg-cyan-500/10 blur-[100px] rounded-full pointer-events-none" />
          
          <div className="relative flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between z-10">
            <div className="flex gap-4">
              <span className="mt-1 flex items-center justify-center rounded-2xl bg-cyan-500/20 p-3.5 text-cyan-400 border border-cyan-500/30 shadow-inner">
                <CloudDownload size={24} />
              </span>
              <div>
                <h2 className="text-xl font-bold tracking-tight text-white drop-shadow-md">UniMelb Echo360</h2>
                <p className="mt-1.5 max-w-2xl text-sm font-medium leading-relaxed text-zinc-300 drop-shadow-sm">
                  Echo360 recordings sync automatically in the background. Use
                  the viewer below to browse your media in your normal session;
                  the Sync button at the top pulls in anything new on demand.
                </p>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <button
                onClick={() => {
                  if (!showEchoViewer) setEchoViewerLoaded(false);
                  setShowEchoViewer((current) => !current);
                }}
                aria-pressed={showEchoViewer}
                className="inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-cyan-600 px-5 text-sm font-bold text-white shadow-lg shadow-cyan-900/40 hover:bg-cyan-500 transition-all hover:scale-105 active:scale-95 duration-200"
              >
                <Play size={16} fill="currentColor" />
                {showEchoViewer ? "Hide Echo360" : "Open Echo360 here"}
              </button>
              {echoCourses.length > 0 && (
                <span className="text-xs font-medium text-cyan-700 dark:text-cyan-300">
                  {echoCourses.length} course{echoCourses.length === 1 ? "" : "s"} linked
                </span>
              )}
            </div>
          </div>

          {showEchoViewer && (
            <div className="mt-5 overflow-hidden rounded-2xl border border-cyan-500/20 bg-transparent shadow-sm">
              <div className="flex flex-col gap-3 border-b border-white/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium">Echo360 Media Library</p>
                  <p className="mt-0.5 text-xs text-zinc-400">
                    Embedded from echo360.net.au using this browser&apos;s session.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => {
                      setEchoViewerLoaded(false);
                      setEchoViewerKey((current) => current + 1);
                    }}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/10 px-2.5 text-xs font-medium"
                  >
                    <RefreshCw size={13} />
                    Reload
                  </button>
                  <a
                    href="https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/4090"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/10 px-2.5 text-xs font-medium"
                    title="Authenticate Echo360 via Canvas Lecture Capture LTI"
                  >
                    <ExternalLink size={13} />
                    Open via Canvas LTI
                  </a>
                </div>
              </div>
              <div className="relative min-h-[420px] bg-transparent">
                {!echoViewerLoaded && (
                  <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-transparent text-sm text-slate-500">
                    <Loader2 size={17} className="mr-2 animate-spin" />
                    Loading Echo360…
                  </div>
                )}
                <iframe
                  key={echoViewerKey}
                  src="https://echo360.net.au/content"
                  title="UniMelb Echo360 Media Library"
                  className="h-[70vh] min-h-[420px] w-full border-0"
                  allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
                  sandbox="allow-downloads allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts allow-storage-access-by-user-activation allow-top-navigation-by-user-activation"
                  referrerPolicy="strict-origin-when-cross-origin"
                  allowFullScreen
                  onLoad={() => setEchoViewerLoaded(true)}
                />
              </div>
              <p className="border-t border-white/10 px-4 py-3 text-xs leading-5 text-zinc-400">
                If UniMelb SSO refuses to appear inside the frame, use “Open in
                normal tab”, sign in with autofill, then return and reload this
                viewer. Viewing stays separate from importing so the learning agent
                only receives recordings you explicitly select.
              </p>
            </div>
          )}

          {echoWaitingLogin && (
            <div className="mt-4 rounded-2xl border border-cyan-500/30 bg-cyan-500/10 p-4 shadow-sm">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-start gap-3">
                  <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cyan-500/20 text-cyan-600 dark:text-cyan-400">
                    <Loader2 size={18} className="animate-spin" />
                  </span>
                  <div>
                    <p className="text-sm font-semibold text-white">
                      Waiting for UniMelb Sign-in…
                    </p>
                    <p className="mt-0.5 text-xs text-zinc-400">
                      Please log in and complete Okta 2FA in the opened UniMelb Canvas tab.
                      UniSync is actively listening for your session ({echoLoginSecondsLeft}s remaining).
                    </p>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void checkAuthNow()}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-cyan-600 px-3 text-xs font-medium text-white hover:bg-cyan-500"
                  >
                    <RefreshCw size={13} />
                    Check Now
                  </button>
                  <button
                    type="button"
                    onClick={cancelEchoLoginWait}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 text-xs font-medium hover:bg-slate-100"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}

          {echoError && (
            <p className="mt-4 rounded-xl bg-red-500/10 px-3 py-2 text-sm text-red-600">
              {echoError}
            </p>
          )}
          {echoMessage && (
            <p className="mt-4 text-sm text-zinc-400">{echoMessage}</p>
          )}

          {(echoConnectorInstalled === false) && (
            <div className="mt-4 rounded-xl border border-white/10 bg-white/5 p-4 shadow-sm">
              <div className="flex gap-3">
                <span className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-lg bg-cyan-500/15 text-cyan-600">
                  <Puzzle size={16} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium">Set up Echo360 import</p>
                    <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-500 border border-amber-500/20">
                      Extension Deprecated
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-zinc-400">
                    Notice: The browser extension method is deprecated. UniSync now syncs autonomously via the built-in Camoufox stealth browser.
                  </p>
                  <div className="mt-3 mb-2 rounded-lg border border-cyan-500/20 bg-cyan-500/10 p-2.5 flex items-center justify-between">
                    <div>
                      <p className="text-xs font-semibold text-cyan-400">Recommended: Camoufox Stealth Browser</p>
                      <p className="text-[11px] text-zinc-400">Zero extension setup — autonomous background sync &amp; Echo360 LTI resolution.</p>
                    </div>
                    <a
                      href={apiUrl("/api/v1/browser/login")}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex h-7 items-center gap-1.5 rounded-md bg-cyan-600 px-2.5 text-xs font-medium text-white hover:bg-cyan-500"
                    >
                      <ExternalLink size={12} />
                      Launch Camoufox Login
                    </a>
                  </div>

                  <div className="mt-4 space-y-0">
                    {[
                      {
                        title: "Install Tampermonkey (Legacy Fallback)",
                        desc: "Deprecated browser extension method.",
                        action: (
                          <a
                            href="https://www.tampermonkey.net/"
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-cyan-600 px-3 text-xs font-medium text-white"
                          >
                            <Download size={13} />
                            Get Tampermonkey
                          </a>
                        ),
                      },
                      {
                        title: "Install the connector script",
                        desc: "Adds the Echo360 import bridge.",
                        action: (
                          <a
                            href={apiUrl("/api/v1/lectures/echo360/connector.user.js")}
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-cyan-600 px-3 text-xs font-medium text-white"
                          >
                            <Download size={13} />
                            Install script
                          </a>
                        ),
                      },
                      {
                        title: "Enable script permissions",
                        desc: 'Chrome 138+ needs Developer Mode at chrome://extensions, or "Allow User Scripts" in Tampermonkey settings.',
                        action: null,
                      },
                      {
                        title: "Launch Echo360 via Canvas LTI",
                        desc: "Open Canvas Lecture Capture (external_tools/4090) to authenticate your Echo360 session via UniMelb LMS SSO.",
                        action: (
                          <a
                            href="https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/4090"
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/10 px-3 text-xs font-medium"
                          >
                            <ExternalLink size={13} />
                            Launch Echo360 (Canvas LTI)
                          </a>
                        ),
                      },
                    ].map((step, index) => (
                      <div key={index} className="flex items-start gap-3 border-b border-white/10 py-3 last:border-0">
                        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-cyan-500/10 text-xs font-semibold text-cyan-600">
                          {index + 1}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="text-sm">{step.title}</p>
                          <p className="mt-0.5 text-xs text-zinc-400">{step.desc}</p>
                        </div>
                        <div className="shrink-0">{step.action}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
              <button
                onClick={() => window.location.reload()}
                className="mt-4 inline-flex h-9 w-full items-center justify-center gap-2 rounded-lg bg-cyan-600 text-sm font-medium text-white"
              >
                <RefreshCw size={14} />
                Reload page &mdash; I&apos;ve completed the steps above
              </button>
            </div>
          )}

          {/* Version mismatch banner */}
          {echoConnectorInstalled === true &&
            echoConnectorVersion &&
            echoExpectedVersion &&
            echoConnectorVersion !== echoExpectedVersion && (
            <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm">
              <div className="flex items-start gap-3">
                <span className="mt-0.5 shrink-0">⚠️</span>
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-amber-900">Connector needs update</p>
                  <p className="mt-1 text-amber-700">
                    Installed version <code className="rounded bg-amber-100 px-1 py-0.5">{echoConnectorVersion}</code>
                    &nbsp;— latest is <code className="rounded bg-amber-100 px-1 py-0.5">{echoExpectedVersion}</code>.
                    Reinstall from the link below to pick up the latest fixes.
                  </p>
                  <a
                    href={apiUrl("/api/v1/lectures/echo360/connector.user.js")}
                    className="mt-2 inline-flex h-8 items-center gap-1.5 rounded-lg bg-amber-600 px-3 text-xs font-medium text-white hover:bg-amber-700"
                  >
                    <Download size={13} />
                    Update script
                  </a>
                </div>
              </div>
            </div>
          )}

          {/* Success Banner */}
          {!!echoSuccess && (
            <div className="mt-4 flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
              <div className="flex items-center gap-2.5">
                <CheckCircle2 size={16} className="shrink-0 text-emerald-500" />
                <span>{echoSuccess}</span>
              </div>
              <button onClick={() => setEchoSuccess("")} className="text-[11px] opacity-75 hover:opacity-100">
                Dismiss
              </button>
            </div>
          )}

          {/* Error Banner */}
          {!!echoError && (
            <div className="mt-4 flex items-center justify-between rounded-xl border border-red-500/30 bg-red-500/10 p-3.5 text-xs font-medium text-red-600 dark:text-red-400">
              <div className="flex items-center gap-2.5">
                <AlertCircle size={16} className="shrink-0 text-red-500" />
                <span>{echoError}</span>
              </div>
              <button onClick={() => setEchoError("")} className="text-[11px] opacity-75 hover:opacity-100">
                Dismiss
              </button>
            </div>
          )}

        </div>

        {/* ── UniMelb Canvas Content ── */}
        <div className="mb-6 overflow-hidden rounded-2xl border border-violet-500/20 bg-black/60 p-6 shadow-2xl backdrop-blur-xl relative">
          <div className="absolute top-0 right-0 p-32 bg-violet-500/10 blur-[100px] rounded-full pointer-events-none" />
          
          <div className="relative flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between z-10">
            <div className="flex gap-4">
              <span className="mt-1 flex items-center justify-center rounded-2xl bg-violet-500/20 p-3.5 text-violet-400 border border-violet-500/30 shadow-inner">
                <Layers size={24} />
              </span>
              <div>
                <h2 className="text-xl font-bold tracking-tight text-white drop-shadow-md">UniMelb Canvas</h2>
                <p className="mt-1.5 max-w-2xl text-sm font-medium leading-relaxed text-zinc-300 drop-shadow-sm">
                  Canvas content — syllabus, pages, files, assignments,
                  discussions, quizzes, modules and embedded Echo360 media —
                  syncs automatically in the background. The Sync button at the
                  top pulls in anything new on demand.
                </p>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <a
                href="https://canvas.lms.unimelb.edu.au/"
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-violet-600 px-5 text-sm font-bold text-white shadow-lg shadow-violet-900/40 hover:bg-violet-500 transition-all hover:scale-105 active:scale-95 duration-200"
              >
                <ExternalLink size={16} />
                Open Canvas
              </a>
              {canvasCourses.length > 0 && (
                <span className="text-xs font-medium text-violet-700 dark:text-violet-300">
                  {canvasCourses.length} course{canvasCourses.length === 1 ? "" : "s"} linked
                </span>
              )}
            </div>
          </div>

          {canvasAuthOk === false && (
            <div className="mt-4 flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3.5 text-xs text-amber-700 dark:text-amber-300">
              <AlertCircle size={16} className="mt-0.5 shrink-0 text-amber-500" />
              <div>
                <p className="font-medium">Canvas session expired</p>
                <p className="mt-0.5 text-zinc-400">
                  Open <a href="https://canvas.lms.unimelb.edu.au" target="_blank" rel="noopener noreferrer" className="underline">canvas.lms.unimelb.edu.au</a> and sign in, then return here. Auto-sync will resume on the next sweep.
                </p>
              </div>
            </div>
          )}

          {canvasError && (
            <p className="mt-4 rounded-xl bg-red-500/10 px-3 py-2 text-sm text-red-600">
              {canvasError}
            </p>
          )}
          {canvasMessage && (
            <p className="mt-4 text-sm text-zinc-400">{canvasMessage}</p>
          )}
          {canvasProgress && (
            <div className="mt-4 rounded-xl border border-violet-500/20 bg-violet-500/5 p-3">
              <div className="mb-1.5 flex items-center justify-between gap-3 text-xs">
                <span className="truncate font-medium text-violet-700 dark:text-violet-300">
                  {canvasProgress.message}
                </span>
                <span className="shrink-0 font-mono text-zinc-400">
                  {canvasProgress.total > 0
                    ? `${Math.min(canvasProgress.done, canvasProgress.total)}/${canvasProgress.total}`
                    : "…"}
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-violet-500/15">
                {canvasProgress.total > 0 ? (
                  <div
                    className="h-full rounded-full bg-violet-500 transition-all duration-300"
                    style={{
                      width: `${Math.min(
                        100,
                        Math.round((canvasProgress.done / canvasProgress.total) * 100),
                      )}%`,
                    }}
                  />
                ) : (
                  <div className="h-full w-1/3 animate-pulse rounded-full bg-violet-400/70" />
                )}
              </div>
            </div>
          )}
          {canvasLog.length > 0 && (
            <details className="mt-3" open={canvasBusy || canvasFilesBusy}>
              <summary className="cursor-pointer select-none text-xs font-medium text-zinc-400 hover:text-white">
                Verbose log ({canvasLog.length})
              </summary>
              <pre className="mt-2 max-h-64 overflow-y-auto rounded-xl border border-white/10 bg-slate-100/40 p-3 font-mono text-[11px] leading-relaxed text-zinc-400">
                {canvasLog.join("\n")}
              </pre>
            </details>
          )}
          {!!canvasSuccess && (
            <div className="mt-4 flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
              <div className="flex items-center gap-2.5">
                <CheckCircle2 size={16} className="shrink-0 text-emerald-500" />
                <span>{canvasSuccess}</span>
              </div>
              <button onClick={() => setCanvasSuccess("")} className="text-[11px] opacity-75 hover:opacity-100">
                Dismiss
              </button>
            </div>
          )}

        </div>

                </div>
      </section>
    </main>
  );
}

/**
 * The sync pipeline now runs in whichever tab wins the connector's GM-storage
 * leader lease (any page the userscript matches), so it survives reloads and
 * even closing the library page — no dedicated worker tab to render.
 */

/**
 * Lazy-loading video thumbnail for episode grid cards.
 * Uses IntersectionObserver to defer the range request until the card is near
 * the viewport, preventing 10+ simultaneous HTTP range fetches on page load.
 */
const LazyVideoThumb = memo(function LazyVideoThumb({ src, className }: { src: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) { setVisible(true); io.disconnect(); } },
      { rootMargin: "400px" }, // start loading ~1 screen before visible
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div ref={ref} className={className}>
      {visible && (
        <video
          src={src}
          preload="metadata"
          className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
          muted
          playsInline
        />
      )}
    </div>
  );
});

export default function MediaLibraryPage() {
  return <MediaLibraryPageInner />;
}