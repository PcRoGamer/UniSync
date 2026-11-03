"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  FileAudio,
  FileVideo,
  Maximize,
  Minimize,
  MonitorPlay,
  Pause,
  PictureInPicture,
  Play,
  Subtitles,
  Waves,
  X,
} from "lucide-react";
import { apiFetch, apiUrl } from "@/lib/api";
import {
  MediaPlayer,
  MediaProvider,
  Track,
  useMediaState,
  type MediaPlayerInstance,
} from "@vidstack/react";
import {
  DefaultVideoLayout,
  defaultLayoutIcons,
} from "@vidstack/react/player/layouts/default";
// Plex: vidstack default theme + layout CSS required for controls to render
import "@vidstack/react/player/styles/default/theme.css";
import "@vidstack/react/player/styles/default/layouts/video.css";
import {
  parseVttToSegments,
  formatTimestamp,
  type SegmentTiming,
  type WordTiming,
} from "@/lib/subtitle-utils";

/* ────────────────────────────────────────────────────────────────────
 * Types
 * ──────────────────────────────────────────────────────────────────── */

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

/** Plex: theatre view modes — cinema removed, pip kept for toggle. */
type ViewMode = "theatre" | "fullscreen" | "pip";

/* ────────────────────────────────────────────────────────────────────
 * Sub-Components — local to this file to avoid polluting shared scope.
 * Mirrors the Nuvio karaoke card aesthetic from library/page.tsx.
 * ──────────────────────────────────────────────────────────────────── */

/**
 * Single TranscriptCard — renders word-by-word timing with seek-on-click.
 * Used inside both the karaoke overlay and the transcript drawer.
 */
function TranscriptCard({
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
          ? "border-cyan-500/40 bg-[#090d16]/95 text-white shadow-2xl backdrop-blur-md px-4 py-2"
          : isActive
            ? "border-cyan-500/50 bg-cyan-500/10 shadow-sm p-2.5"
            : "border-transparent bg-white/[0.04] hover:border-white/15 hover:bg-white/[0.08] p-2.5"
      }`}
    >
      {/* Timestamp row — hidden in overlay for minimal height */}
      {!overlayMode && (
        <div className="mb-1 flex items-center justify-between text-[10px] text-zinc-500">
          <button
            type="button"
            onClick={() => onSeek?.(segment.start)}
            className="font-mono font-semibold text-cyan-500 hover:underline cursor-pointer"
            title="Jump to this sentence"
          >
            {formatTimestamp(segment.start)}
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
        {segment.words && segment.words.length > 0
          ? segment.words.map((w, wIdx) => {
              const isWordActive =
                currentTime >= w.start && currentTime <= w.end;
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
                      ? "bg-cyan-500/25 text-cyan-400 font-bold"
                      : isWordPassed
                        ? overlayMode
                          ? "text-white"
                          : "text-zinc-300"
                        : overlayMode
                          ? "text-zinc-400/60"
                          : "text-zinc-400/60 hover:text-zinc-200"
                  }`}
                  title={`Jump to ${w.text} (${w.start.toFixed(1)}s)`}
                >
                  {w.text}
                </span>
              );
            })
          : (
            <span
              onClick={() => onSeek?.(segment.start)}
              className="cursor-pointer text-zinc-300"
            >
              {segment.text}
            </span>
          )}
      </div>
    </div>
  );
}

/**
 * KaraokeSubtitleOverlay — floats active sentence above the video.
 * Uses Vidstack's `useMediaState` for high-frequency (60fps) currentTime synchronization
 * so individual word highlights never skip short words or desync during playback.
 * Repositions smoothly when the player control bar appears.
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
  // Always prefer Vidstack's internal high-frequency clock when available
  const currentTime =
    propCurrentTime !== undefined ? propCurrentTime : mediaCurrentTime;
  const currentTextTrack = useMediaState("textTrack");
  const textTracks = useMediaState("textTracks");
  const controlsVisible = useMediaState("controlsVisible");

  // In Vidstack, verify that captions are currently active (mode === 'showing' or textTrack is set)
  const isCaptionsActive =
    currentTextTrack !== null ||
    (Array.isArray(textTracks)
      ? textTracks.some((t) => t && t.mode === "showing")
      : Boolean(textTracks && (textTracks as any).length > 0 && Array.from(textTracks as any).some((t: any) => t && t.mode === "showing")));

  const activeSegment = segments.find(
    (s) => currentTime >= s.start - 0.05 && currentTime <= s.end + 0.15,
  );

  if (!isCaptionsActive || !activeSegment || !activeSegment.text) return null;

  return (
    <div
      className={`pointer-events-none absolute left-4 right-4 z-20 flex justify-center text-left transition-all duration-200 ${
        controlsVisible ? "bottom-24 sm:bottom-28" : "bottom-12 sm:bottom-14"
      }`}
    >
      <div className="max-w-[92%] sm:max-w-[620px] w-full pointer-events-auto">
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
 * IntroSkipOverlay — shows "Skip Intro" button when within intro window.
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

  if (introDuration <= 0 || currentTime >= introDuration) return null;

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
      </button>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
 * Main TheatrePage Component
 * ──────────────────────────────────────────────────────────────────── */

export default function TheatrePage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  /* ── State ── */
  const [item, setItem] = useState<MediaItem | null>(null);
  const [allItems, setAllItems] = useState<MediaItem[]>([]);
  const [segments, setSegments] = useState<SegmentTiming[]>([]);
  const [currentTime, setCurrentTime] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>("theatre");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [navVisible, setNavVisible] = useState(true);
  const [loading, setLoading] = useState(true);
  const [scrolled, setScrolled] = useState(false);
  const [autoSkipIntro, setAutoSkipIntro] = useState<boolean>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("unisync_auto_skip_intro") !== "false";
    }
    return true;
  });

  // Plex: theatre — active episode scroll-to-view
  const activeEpisodeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 40);
    };
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  const playerRef = useRef<MediaPlayerInstance>(null);
  const navTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastProgressSaveRef = useRef(0);
  const pendingSeekRef = useRef<number | null>(null);

  /* ── Fetch item data + all items for episode rail ── */
  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      try {
        const [itemRes, allRes] = await Promise.all([
          apiFetch(apiUrl(`/api/v1/lectures/${id}`), { cache: "no-store" }),
          apiFetch(apiUrl("/api/v1/lectures"), { cache: "no-store" }),
        ]);

        if (cancelled) return;

        if (itemRes.ok) {
          const data = (await itemRes.json()) as MediaItem;
          setItem(data);
          // Resume position
          const pos = data.position_seconds || 0;
          const dur = data.duration_seconds || 0;
          if (pos > 5 && (!dur || pos < dur - 30)) {
            pendingSeekRef.current = pos;
          }
        }
        if (allRes.ok) {
          const data = (await allRes.json()) as { items: MediaItem[] };
          setAllItems(data.items);
        }
      } catch {
        // Backend unreachable
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => { cancelled = true; };
  }, [id]);

  /* ── Fetch word-level karaoke segments ── */
  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function loadWords() {
      try {
        const res = await apiFetch(apiUrl(`/api/v1/lectures/${id}/words`));
        if (res.ok) {
          const data = await res.json();
          if (!cancelled && data?.segments?.length > 0) {
            setSegments(data.segments);
            return;
          }
        }
      } catch { /* fall through */ }

      try {
        const subRes = await apiFetch(apiUrl(`/api/v1/lectures/${id}/subtitles`));
        if (subRes.ok) {
          const vttText = await subRes.text();
          const parsed = parseVttToSegments(vttText);
          if (!cancelled && parsed.length > 0) {
            setSegments(parsed);
          }
        }
      } catch { /* graceful */ }
    }

    void loadWords();
    return () => { cancelled = true; };
  }, [id]);

  /* ── Plex: theatre — Episode rail: same series_name, course_name, or kb_name (embeds & courses) ── */
  const relatedEpisodes = useMemo(() => {
    if (!item || allItems.length === 0) return [];
    // Match by series_name, course_name, or kb_name so embeds and course media are grouped into the rail
    const key = item.series_name || item.course_name || item.kb_name || "";
    if (!key) return [];
    const related = allItems
      .filter(
        (i) =>
          (i.series_name || i.course_name || i.kb_name || "") === key &&
          (i.status === "ready" || i.status === "downloaded"),
      )
      .sort((a, b) =>
        (a.series_date || a.created_at || "").localeCompare(
          b.series_date || b.created_at || "",
        ),
      );
    /* Keep chronological order — the rail auto-scrolls to the active episode.
       Original episode numbers are preserved in the badge (idx + 1). */
    return related;
  }, [item, allItems]);

  /* ── Plex: theatre — Auto-hide nav ── */
  const showNav = useCallback(() => {
    setNavVisible(true);
    if (navTimerRef.current) clearTimeout(navTimerRef.current);
    navTimerRef.current = setTimeout(() => {
      setNavVisible(false);
    }, 2500);
  }, []);

  // Track controls visibility from Vidstack to keep nav alive when controls are up
  const controlsVisible = useMediaState("controlsVisible");

  useEffect(() => {
    if (controlsVisible) {
      setNavVisible(true);
      if (navTimerRef.current) clearTimeout(navTimerRef.current);
      navTimerRef.current = setTimeout(() => {
        setNavVisible(false);
      }, 3000);
    }
  }, [controlsVisible]);

  useEffect(() => {
    return () => {
      if (navTimerRef.current) clearTimeout(navTimerRef.current);
    };
  }, []);

  /* Plex: auto-scroll the episode rail to center the active episode on mount
     and whenever the user navigates to a different episode. Uses
     requestAnimationFrame to ensure the DOM has rendered the rail first. */
  useEffect(() => {
    requestAnimationFrame(() => {
      activeEpisodeRef.current?.scrollIntoView({
        block: "nearest",
        inline: "center",
        behavior: "smooth",
      });
    });
  }, [id]);

  /* ── Progress saving ── */
  const saveProgress = useCallback(
    async (position: number, duration?: number) => {
      if (!id) return;
      try {
        await apiFetch(apiUrl(`/api/v1/lectures/${id}/progress`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            position_seconds: position,
            duration_seconds: duration ?? null,
          }),
        });
      } catch { /* best-effort */ }
    },
    [id],
  );

  const handleTimeUpdate = useCallback(
    (time: number, duration?: number) => {
      setCurrentTime(time);
      const now = Date.now();
      if (now - lastProgressSaveRef.current > 5000) {
        lastProgressSaveRef.current = now;
        const effective =
          duration && time >= duration - 3 ? 0 : time;
        void saveProgress(effective, duration);
      }
    },
    [saveProgress],
  );

  /* ── Plex: theatre — Fix fullscreen via document root (Vidstack proxy doesn't expose requestFullscreen) ── */
  const enterFullscreen = useCallback(async () => {
    try {
      await document.documentElement.requestFullscreen();
      setViewMode("fullscreen");
    } catch { /* user denied or not supported */ }
  }, []);

  const exitFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    setViewMode("theatre");
  }, []);

  /* ── Plex: theatre — Fix PiP: try media player internal video, fallback to DOM query ── */
  const togglePiP = useCallback(async () => {
    if (!document.pictureInPictureEnabled) return;
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
        setViewMode("theatre");
        return;
      }
      /* MediaPlayerInstance wraps a <video> — try Vidstack's internal ref first,
         then fall back to a DOM query on the player wrapper element. */
      const video = (playerRef.current as any)?.video
        ?? (playerRef.current as any)?.el?.querySelector?.("video")
        ?? (playerRef.current as unknown as HTMLElement)?.querySelector?.("video") as
          HTMLVideoElement | null;
      if (video) {
        await video.requestPictureInPicture();
        setViewMode("pip");
      }
    } catch { /* not supported or denied — keep current mode */ }
  }, []);

  // Respond to fullscreen change events (e.g. user presses Esc)
  useEffect(() => {
    const handler = () => {
      if (!document.fullscreenElement && viewMode === "fullscreen") {
        setViewMode("theatre");
        setNavVisible(true);
      }
    };
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, [viewMode]);

  /* ── Keyboard Shortcuts ── */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Don't intercept when user is typing in an input/textarea
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      const video = playerRef.current;
      if (!video) return;

      switch (e.key.toLowerCase()) {
        case " ":
        case "k":
          e.preventDefault();
          if (video.paused) video.play();
          else video.pause();
          break;
        case "arrowleft":
          e.preventDefault();
          video.currentTime = Math.max(0, video.currentTime - 10);
          showNav();
          break;
        case "arrowright":
          e.preventDefault();
          video.currentTime = Math.min(
            video.duration || Infinity,
            video.currentTime + 10,
          );
          showNav();
          break;
        case "arrowup":
          e.preventDefault();
          video.currentTime = Math.min(
            video.duration || Infinity,
            video.currentTime + 60,
          );
          showNav();
          break;
        case "arrowdown":
          e.preventDefault();
          video.currentTime = Math.max(0, video.currentTime - 60);
          showNav();
          break;
        case "f":
          e.preventDefault();
          if (viewMode === "fullscreen") exitFullscreen();
          else enterFullscreen();
          break;
        case "t":
          e.preventDefault();
          setDrawerOpen((prev) => !prev);
          break;
        case "p":
          e.preventDefault();
          togglePiP();
          break;
        case "escape":
          if (viewMode === "fullscreen") exitFullscreen();
          else if (drawerOpen) setDrawerOpen(false);
          break;
      }
    };

    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [
    viewMode,
    drawerOpen,
    showNav,
    enterFullscreen,
    exitFullscreen,
    togglePiP,
  ]);

  /* ── Navigate between episodes ── */
  const currentIndex = useMemo(
    () => relatedEpisodes.findIndex((e) => e.id === id),
    [relatedEpisodes, id],
  );

  const goNext = useCallback(() => {
    if (currentIndex >= 0 && currentIndex < relatedEpisodes.length - 1) {
      router.push(`/library/${relatedEpisodes[currentIndex + 1].id}`);
    }
  }, [currentIndex, relatedEpisodes, router]);

  const goPrev = useCallback(() => {
    if (currentIndex > 0) {
      router.push(`/library/${relatedEpisodes[currentIndex - 1].id}`);
    }
  }, [currentIndex, relatedEpisodes, router]);

  /* ── Loading state ── */
  if (loading || !item) {
    return (
      <div className="flex h-screen items-center justify-center bg-[#0f0f10]">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan-500 border-t-transparent" />
          <span className="text-sm text-zinc-400">Loading…</span>
        </div>
      </div>
    );
  }

  /* ── Derived values ── */
  const isVideo = item.content_type.startsWith("video/");
  const isFullscreen = viewMode === "fullscreen";

  return (
    <div
      // Plex: allow scrolling in non-fullscreen mode to view episode rail
      className={`relative flex flex-col bg-[#0f0f10] text-zinc-200 selection:bg-cyan-500/30 font-sans ${
        isFullscreen ? "h-screen overflow-hidden" : "h-full min-h-screen overflow-y-auto"
      }`}
      onMouseMove={showNav}
      onPointerMove={showNav}
    >
      {/* Sidebar-hiding, subtitle deduplication, and high-visibility Vidstack styling */}
      <style dangerouslySetInnerHTML={{ __html: `
        .beach-surface { background-color: #0f0f10 !important; }
        .ocean-sidebar-root { display: none !important; }
        .pebble-conversation-switcher-root { display: none !important; }
        .vds-captions, [data-part="captions"], .vds-caption-renderer { display: none !important; }
        
        /* High-visibility Vidstack Controls */
        .vds-video-layout {
          --video-brand: #06b6d4 !important;
          --video-controls-color: #ffffff !important;
          z-index: 25 !important;
        }
        .vds-controls {
          z-index: 25 !important;
          opacity: 1 !important;
          transition: opacity 0.2s ease-in-out !important;
        }
        .vds-controls-group {
          padding: 8px 16px !important;
          background: linear-gradient(to top, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 60%, transparent 100%) !important;
        }
        .vds-button {
          cursor: pointer !important;
        }
        .vds-time-slider {
          --slider-height: 4px !important;
          --slider-track-color: rgba(255, 255, 255, 0.3) !important;
          --slider-fill-color: #06b6d4 !important;
        }
        .vds-time-slider:hover {
          --slider-height: 6px !important;
        }
      `}} />

      {/* ─── Auto-hide Top Nav ─── */}
      <nav
        className={`fixed top-0 inset-x-0 z-50 flex items-center justify-between px-4 sm:px-6 py-3 transition-all duration-300 ${
          navVisible
            ? "opacity-100 translate-y-0"
            : "opacity-0 -translate-y-4 pointer-events-none"
        } bg-gradient-to-b from-[#0f0f10] via-[#0f0f10]/80 to-transparent backdrop-blur-sm`}
      >
        {/* Left: Back + Nuvio */}
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => router.push("/library")}
            className="flex items-center gap-2 text-zinc-400 hover:text-white transition-colors"
            title="Back to Library"
          >
            <ArrowLeft size={18} />
            <Waves size={20} strokeWidth={3} className="text-cyan-500" />
          </button>
          <div className="hidden sm:block h-5 w-px bg-white/10" />
          <div className="hidden sm:flex flex-col min-w-0">
            <span className="text-sm font-semibold text-white truncate max-w-[320px]">
              {item.title}
            </span>
            {item.course_name && (
              <span className="text-[11px] text-zinc-400 truncate max-w-[320px]">
                {item.course_name}
              </span>
            )}
          </div>
        </div>

        {/* Right: View Mode Toggle + Drawer Toggle */}
        <div className="flex items-center gap-1.5">
          {/* View mode pills */}
          <div className="hidden sm:flex items-center gap-1 rounded-lg bg-white/5 border border-white/10 p-0.5 text-[11px] font-medium">
            <button
              type="button"
              onClick={() => { setViewMode("theatre"); }}
              className={`flex items-center gap-1 rounded-md px-2 py-1 transition cursor-pointer ${
                viewMode === "theatre"
                  ? "bg-white/10 text-white"
                  : "text-zinc-400 hover:text-white"
              }`}
              title="Theatre (default layout)"
            >
              <MonitorPlay size={12} />
              Theatre
            </button>
            <button
              type="button"
              onClick={() => { if (isFullscreen) exitFullscreen(); else enterFullscreen(); }}
              className={`flex items-center gap-1 rounded-md px-2 py-1 transition cursor-pointer ${
                isFullscreen
                  ? "bg-cyan-600 text-white"
                  : "text-zinc-400 hover:text-white"
              }`}
              title="Fullscreen (F)"
            >
              <Maximize size={12} />
            </button>
            <button
              type="button"
              onClick={togglePiP}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-zinc-400 hover:text-white transition cursor-pointer"
              title="Picture-in-Picture (P)"
            >
              <PictureInPicture size={12} />
            </button>
          </div>

          <div className="h-5 w-px bg-white/10 hidden sm:block" />

          {/* Transcript drawer toggle */}
          <button
            type="button"
            onClick={() => setDrawerOpen((prev) => !prev)}
            className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition cursor-pointer ${
              drawerOpen
                ? "bg-cyan-600 text-white"
                : "bg-white/5 border border-white/10 text-zinc-400 hover:text-white"
            }`}
            title="Toggle Transcript (T)"
          >
            <Subtitles size={14} />
            <span className="hidden sm:inline">Transcript</span>
          </button>
        </div>
      </nav>

            {/* ─── Main Viewport: Fullscreen Cinema Video Player (Sticky 100vh) ─── */}
      <div className="relative w-full h-screen sticky top-0 z-10 flex bg-black overflow-hidden select-none">
        {/* Video / Audio Player Container */}
        <div className="relative flex-1 h-full w-full flex items-center justify-center bg-black overflow-hidden">
          {isVideo ? (
            <div className="relative w-full h-full flex items-center justify-center">
              {item.status !== "ready" && item.status !== "downloaded" ? (
                /* Media not yet available — show download status */
                <div className="flex h-full flex-col items-center justify-center gap-4 bg-black p-8 text-center">
                  <div className="h-10 w-10 animate-spin rounded-full border-2 border-cyan-500 border-t-transparent" />
                  <p className="text-sm font-medium text-zinc-300">
                    {item.status === "queued"
                      ? "Media queued for download…"
                      : item.status === "processing"
                        ? "Processing media…"
                        : "Preparing media…"}
                  </p>
                  {item.progress && item.progress !== "Queued" && (
                    <p className="text-xs text-zinc-500">{item.progress}</p>
                  )}
                  <p className="text-[11px] text-zinc-600">
                    The player will appear automatically when the media is ready.
                  </p>
                </div>
              ) : (
                <MediaPlayer
                  ref={playerRef}
                  key={id}
                  src={{
                    src: apiUrl(`/api/v1/lectures/${id}/media`),
                    type: (item.content_type || "video/mp4") as "video/mp4",
                  }}
                  title={item.title}
                  controls
                  autoPlay
                  playsInline
                  className="w-full h-full object-contain"
                  onTimeUpdate={(e) =>
                    handleTimeUpdate(
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
                      src={apiUrl(
                        `/api/v1/lectures/${id}/subtitles?mode=karaoke`,
                      )}
                      lang="en"
                      label="Word-by-Word Karaoke"
                      default
                    />
                    <Track
                      kind="captions"
                      src={apiUrl(
                        `/api/v1/lectures/${id}/subtitles?mode=standard`,
                      )}
                      lang="en-US"
                      label="Standard Subtitles (Line-by-Line)"
                    />
                  </MediaProvider>
                  <DefaultVideoLayout
                    thumbnails={apiUrl(`/api/v1/lectures/${id}/thumbnails.vtt`)}
                    slots={{}}
                    icons={defaultLayoutIcons}
                  />
                  <KaraokeSubtitleOverlay
                    segments={segments}
                    onSeek={(time) => {
                      if (playerRef.current) {
                        playerRef.current.currentTime = time;
                      }
                    }}
                  />
                  <IntroSkipOverlay
                    introDuration={
                      item.intro_duration ||
                      (item.source === "echo360" ? 14.0 : 0)
                    }
                    autoSkip={autoSkipIntro}
                    onSkip={() => {
                      if (playerRef.current) {
                        playerRef.current.currentTime =
                          item.intro_duration || 14.0;
                      }
                    }}
                  />
                </MediaPlayer>
              )}

              {/* Scroll prompt cue - visible only when at top of page and series has multiple episodes or notes */}
              {!scrolled && !isFullscreen && (relatedEpisodes.length > 1 || Boolean(item.summary)) && (
                <button
                  type="button"
                  onClick={() => {
                    window.scrollTo({ top: window.innerHeight * 0.7, behavior: "smooth" });
                  }}
                  className="absolute bottom-16 sm:bottom-20 left-1/2 -translate-x-1/2 z-20 flex flex-col items-center gap-1 text-white/70 hover:text-cyan-400 transition-all duration-300 animate-bounce cursor-pointer group pointer-events-auto"
                >
                  <span className="text-[10px] font-semibold uppercase tracking-widest bg-black/80 px-3 py-1 rounded-full backdrop-blur-md border border-white/20 group-hover:border-cyan-500/50 shadow-lg">
                    Scroll for Episodes & Notes
                  </span>
                  <ChevronDown size={16} />
                </button>
              )}
            </div>
          ) : (
            /* Audio player fallback */
            <div className="flex h-full flex-col items-center justify-center p-8">
              <div className="mb-6 text-6xl text-zinc-700">
                <FileAudio size={80} />
              </div>
              <audio
                controls
                autoPlay
                className="w-full max-w-lg"
                src={apiUrl(`/api/v1/lectures/${id}/media`)}
                onTimeUpdate={(e) =>
                  handleTimeUpdate(
                    e.currentTarget.currentTime,
                    e.currentTarget.duration,
                  )
                }
                onLoadedMetadata={(e) => {
                  const target = pendingSeekRef.current;
                  if (target != null) {
                    e.currentTarget.currentTime = target;
                    pendingSeekRef.current = null;
                  }
                }}
              />
              {/* Inline audio karaoke display */}
              {segments.length > 0 && (() => {
                const seg = segments.find(
                  (s) =>
                    currentTime >= s.start - 0.05 &&
                    currentTime <= s.end + 0.15,
                );
                if (!seg) return null;
                return (
                  <div className="mt-8 max-w-lg text-center">
                    <div className="inline-block rounded-2xl border border-white/15 bg-[#090d16]/92 px-6 py-4 text-lg font-bold text-white shadow-2xl backdrop-blur-md">
                      {seg.words.map((w, idx) => {
                        const isActive =
                          currentTime >= w.start &&
                          currentTime <= w.end;
                        const isPassed = currentTime > w.end;
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
                      })}
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>

        {/* ─── Slide-out Live Transcript Drawer ─── */}
        <div
          className={`shrink-0 overflow-hidden transition-all duration-300 ease-out ${
            drawerOpen ? "w-[380px] border-l border-white/10" : "w-0"
          }`}
        >
          <div className="flex h-full w-[380px] flex-col bg-[#0a0a0c]/95 backdrop-blur-xl">
            {/* Drawer header */}
            <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
              <div className="flex items-center gap-2">
                <Subtitles size={16} className="text-cyan-500" />
                <span className="text-sm font-semibold text-white">
                  Live Transcript
                </span>
                <span className="rounded bg-cyan-500/15 px-1.5 py-0.5 text-[10px] font-bold text-cyan-500">
                  {segments.length}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                className="rounded-lg p-1 text-zinc-400 hover:text-white hover:bg-white/10 transition-colors cursor-pointer"
                title="Close (Esc)"
              >
                <X size={16} />
              </button>
            </div>

            {/* Auto-skip toggle */}
            {isVideo && (
              <div className="flex items-center justify-between border-b border-white/10 px-4 py-2">
                <button
                  type="button"
                  onClick={() => {
                    const next = !autoSkipIntro;
                    setAutoSkipIntro(next);
                    if (typeof window !== "undefined") {
                      localStorage.setItem(
                        "unisync_auto_skip_intro",
                        next ? "true" : "false",
                      );
                    }
                  }}
                  className={`inline-flex h-6 items-center gap-1 rounded-full border px-2 text-[10px] font-medium transition cursor-pointer ${
                    autoSkipIntro
                      ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-400"
                      : "border-white/10 bg-white/5 text-zinc-400"
                  }`}
                  title="Auto-skip intro disclaimer"
                >
                  Auto-skip Intro
                </button>
              </div>
            )}

            {/* Transcript segments */}
            <div className="flex-1 overflow-y-auto px-3 py-3 space-y-1.5">
              {segments.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <Subtitles
                    size={32}
                    className="mb-3 text-zinc-600"
                  />
                  <span className="text-xs text-zinc-500">
                    No transcript available yet.
                  </span>
                  <span className="mt-1 text-[10px] text-zinc-600">
                    Transcripts appear once processing completes.
                  </span>
                </div>
              ) : (
                segments.map((seg, idx) => (
                  <TranscriptCard
                    key={idx}
                    segment={seg}
                    currentTime={currentTime}
                    isActive={
                      currentTime >= seg.start - 0.05 &&
                      currentTime <= seg.end + 0.15
                    }
                    onSeek={(time) => {
                      if (playerRef.current) {
                        playerRef.current.currentTime = time;
                      }
                    }}
                  />
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ─── Episodes & Lecture Notes Section (Clean natural flow below media player) ─── */}
      {!isFullscreen && (
        <div className="relative z-10 w-full min-h-[50vh] bg-gradient-to-b from-[#090d16] via-[#090d16] to-[#0a0e1a] backdrop-blur-2xl border-t border-white/10 pt-6 pb-20 px-4 sm:px-8">
          {/* Scroll separator / handle */}
          <div className="flex justify-center mb-6">
            <div className="w-12 h-1 rounded-full bg-white/20 hover:bg-cyan-500/50 transition-colors" />
          </div>

          {/* Episode Rail (if series has multiple episodes) */}
          {relatedEpisodes.length > 1 && (
            <div className="mb-10">
              {/* Series header */}
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3 min-w-0">
                  <span className="text-sm font-bold text-white truncate">
                    {item.series_name || item.course_name || "Series Episodes"}
                  </span>
                  <span className="rounded-full bg-cyan-500/15 px-2 py-0.5 text-[11px] font-semibold text-cyan-400">
                    {relatedEpisodes.length} Episodes
                  </span>
                </div>
                <div className="flex items-center gap-1.5 text-xs text-zinc-400">
                  <span>Current:</span>
                  <span className="text-cyan-400 font-bold">{currentIndex + 1}</span>
                  <span>/</span>
                  <span>{relatedEpisodes.length}</span>
                </div>
              </div>

              {/* Horizontal scroll rail */}
              <div className="flex gap-4 overflow-x-auto pb-4 scrollbar-thin">
                {relatedEpisodes.map((ep, idx) => {
                  const isActive = ep.id === id;
                  const pct = ep.duration_seconds
                    ? Math.min(
                        (ep.position_seconds || 0) / ep.duration_seconds,
                        1,
                      )
                    : 0;
                  const resume = pct > 0.01 && pct < 0.97;

                  return (
                    <button
                      key={ep.id}
                      ref={isActive ? activeEpisodeRef : undefined}
                      type="button"
                      onClick={() => router.push(`/library/${ep.id}`)}
                      className={`group relative shrink-0 w-[220px] sm:w-[260px] cursor-pointer text-left transition-all duration-200 ${
                        isActive
                          ? "ring-2 ring-cyan-500/80 rounded-xl scale-[1.02] shadow-lg shadow-cyan-500/10"
                          : "hover:scale-[1.02] opacity-80 hover:opacity-100"
                      }`}
                    >
                      {/* Thumbnail */}
                      <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-zinc-900 border border-white/10">
                        {ep.content_type.startsWith("video/") ? (
                          <video
                            src={apiUrl(
                              `/api/v1/lectures/${ep.id}/media#t=120.0`,
                            )}
                            preload="metadata"
                            className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
                            muted
                            playsInline
                          />
                        ) : (
                          <div className="flex h-full items-center justify-center bg-zinc-900 text-zinc-600">
                            <FileAudio size={24} />
                          </div>
                        )}

                        {/* Active indicator */}
                        {isActive && (
                          <div className="absolute inset-0 flex items-center justify-center bg-black/40">
                            <div className="h-8 w-8 rounded-full bg-cyan-500/90 flex items-center justify-center shadow-lg">
                              <Play
                                size={14}
                                fill="currentColor"
                                className="text-white ml-0.5"
                              />
                            </div>
                          </div>
                        )}

                        {/* Episode number badge */}
                        <div className="absolute top-1.5 left-1.5 z-10">
                          <span className="rounded bg-black/70 px-1.5 py-0.5 text-[9px] font-bold text-white/90 backdrop-blur-sm border border-white/10">
                            EP {idx + 1}
                          </span>
                        </div>

                        {/* Progress bar */}
                        {resume && (
                          <div className="absolute inset-x-0 bottom-0 z-10 h-1.5 bg-black/50">
                            <span
                              className="block h-full bg-cyan-500 rounded-r-sm"
                              style={{
                                width: `${Math.round(pct * 100)}%`,
                              }}
                            />
                          </div>
                        )}
                      </div>

                      {/* Episode title */}
                      <div className="mt-2.5 px-0.5">
                        <span
                          className={`text-xs font-semibold leading-snug line-clamp-2 ${
                            isActive
                              ? "text-cyan-400"
                              : "text-zinc-300 group-hover:text-white"
                          } transition-colors`}
                        >
                          {ep.title}
                        </span>
                        {ep.series_date && (
                          <span className="mt-1 block text-[11px] text-zinc-500">
                            {new Date(ep.series_date).toLocaleDateString(undefined, {
                              year: "numeric",
                              month: "short",
                              day: "numeric",
                            })}
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Lecture Summary / Study Notes Card */}
          <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-6 sm:p-8 backdrop-blur-md max-w-4xl">
            <div className="flex items-center justify-between mb-4 border-b border-white/10 pb-4">
              <div>
                <h2 className="text-lg font-bold text-white mb-1">
                  {item.title}
                </h2>
                {item.course_name && (
                  <p className="text-xs text-cyan-400 font-medium">
                    {item.course_name}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen(true)}
                className="flex items-center gap-2 rounded-xl bg-cyan-500/10 border border-cyan-500/30 px-3.5 py-2 text-xs font-semibold text-cyan-400 hover:bg-cyan-500/20 transition cursor-pointer"
              >
                <Subtitles size={14} />
                <span>Open Live Transcript</span>
              </button>
            </div>

            {item.summary ? (
              <div className="prose prose-invert max-w-none text-sm text-zinc-300 leading-relaxed whitespace-pre-wrap">
                {item.summary}
              </div>
            ) : (
              <p className="text-xs text-zinc-500 italic">
                AI lecture summary and structured notes are processing or ready in the knowledge base.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
