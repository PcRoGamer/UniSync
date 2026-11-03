"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, ExternalLink, GraduationCap, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiUrl } from "@/lib/api";
import { useSettings } from "@/components/settings/SettingsContext";
import {
  SettingRow,
  SettingSection,
  SettingsPageHeader,
  inputClass,
} from "@/components/settings/shared";

interface EdStemCourse {
  id: string;
  name: string;
  code?: string;
  year?: string;
  session?: string;
}

interface EdStemSettingsResponse {
  settings?: { api_base_url?: string };
  api_token_set?: boolean;
}

export default function EdStemSettingsPage() {
  const { t } = useTranslation();
  const { registerExtension } = useSettings();
  const [token, setToken] = useState("");
  const [tokenSet, setTokenSet] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [courses, setCourses] = useState<EdStemCourse[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const touchedRef = useRef(false);
  const tokenRef = useRef("");

  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void apiFetch(apiUrl("/api/v1/edstem/settings"))
      .then(async (response) => {
        if (!response.ok) throw new Error(t("Could not load EdStem settings."));
        return response.json() as Promise<EdStemSettingsResponse>;
      })
      .then((data) => {
        if (cancelled) return;
        setTokenSet(data.api_token_set === true);
        setLoaded(true);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : t("Could not load EdStem settings."));
          setLoaded(true);
        }
      });
    return () => { cancelled = true; };
  }, [t]);

  const save = useCallback(async () => {
    if (!touchedRef.current) return;
    setSaving(true);
    setError("");
    try {
      const response = await apiFetch(apiUrl("/api/v1/edstem/settings"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_token: tokenRef.current }),
      });
      const data = (await response.json()) as { api_token_set?: boolean; detail?: string };
      if (!response.ok) throw new Error(data.detail || t("Could not save EdStem settings."));
      setTokenSet(data.api_token_set === true);
      setToken("");
      tokenRef.current = "";
      touchedRef.current = false;
      setMessage(t("EdStem token saved securely."));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("Could not save EdStem settings."));
      throw reason;
    } finally {
      setSaving(false);
    }
  }, [t]);

  const dirty = loaded && touchedRef.current;
  useEffect(() => {
    registerExtension("edstem", { dirty, save });
    return () => registerExtension("edstem", null);
  }, [dirty, registerExtension, save]);

  const updateToken = (value: string) => {
    touchedRef.current = true;
    tokenRef.current = value;
    setToken(value);
    setMessage("");
  };

  const test = async () => {
    setTesting(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch(apiUrl("/api/v1/edstem/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_token: touchedRef.current ? token : null }),
      });
      const data = (await response.json()) as { ok?: boolean; message?: string; detail?: string };
      if (!response.ok || !data.ok) throw new Error(data.message || data.detail || t("EdStem connection failed."));
      setMessage(data.message || t("EdStem connection succeeded."));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("EdStem connection failed."));
    } finally {
      setTesting(false);
    }
  };

  const discover = async () => {
    setDiscovering(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch(apiUrl("/api/v1/edstem/courses"));
      const data = (await response.json()) as { courses?: EdStemCourse[]; detail?: string };
      if (!response.ok) throw new Error(data.detail || t("Could not discover EdStem courses."));
      setCourses(data.courses || []);
      setMessage(t("Found {{count}} enrolled EdStem course(s).", { count: data.courses?.length || 0 }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("Could not discover EdStem courses."));
    } finally {
      setDiscovering(false);
    }
  };

  const register = async () => {
    setDiscovering(true);
    setError("");
    try {
      const response = await apiFetch(apiUrl("/api/v1/edstem/auto-register"), { method: "POST" });
      const data = (await response.json()) as { courses?: EdStemCourse[]; detail?: string };
      if (!response.ok) throw new Error(data.detail || t("Could not register EdStem courses."));
      setCourses(data.courses || courses);
      setMessage(t("EdStem courses linked to your course libraries."));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("Could not register EdStem courses."));
    } finally {
      setDiscovering(false);
    }
  };

  const syncAll = async () => {
    setSyncing(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch(apiUrl("/api/v1/edstem/sync"), { method: "POST" });
      const data = (await response.json()) as { ok?: boolean; synced_courses?: any[]; detail?: string };
      if (!response.ok || !data.ok) throw new Error(data.detail || t("Could not sync EdStem courses."));
      const count = data.synced_courses?.length || 0;
      setMessage(t("Successfully synced all materials for {{count}} EdStem course(s).", { count }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("Could not sync EdStem courses."));
    } finally {
      setSyncing(false);
    }
  };

  if (!loaded) {
    return <div className="grid h-[60vh] place-items-center text-[var(--muted-foreground)]"><Loader2 className="h-4 w-4 animate-spin" /></div>;
  }

  return (
    <div>
      <SettingsPageHeader
        title={t("EdStem")}
        description={t("Sync lessons, resources, discussions, announcements, and due dates from every enrolled EdStem course.")}
      />

      <SettingSection title={t("Connection")} description={t("EdStem uses a server-side read-only API connection so background sync works without an open Ed tab.")}>
        <SettingRow
          title={t("API token")}
          description={tokenSet ? t("A token is configured. Enter a replacement to rotate it, or leave blank to keep the current token.") : t("Create a token in EdStem, then paste it here. It is never shown again after saving.")}
          control={<input type="password" autoComplete="new-password" value={token} onChange={(event) => updateToken(event.target.value)} placeholder={tokenSet ? "Configured — enter replacement" : "Paste EdStem API token"} className={inputClass + " w-72"} />}
        />
        <SettingRow
          title={t("Token page")}
          description={t("Open EdStem's API token settings in a new tab.")}
          control={<a href="https://edstem.org/au/settings/api-tokens" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[13px] font-medium"><ExternalLink className="h-3.5 w-3.5" />{t("Open EdStem")}</a>}
        />
        <SettingRow
          title={t("Actions")}
          control={<div className="flex flex-wrap items-center gap-2"><button type="button" onClick={() => void save()} disabled={saving || !dirty} className="rounded-lg bg-[var(--primary)] px-3.5 py-2 text-[13px] font-medium text-[var(--primary-foreground)] disabled:opacity-40">{saving ? t("Saving…") : t("Save token")}</button><button type="button" onClick={() => void test()} disabled={testing || (!tokenSet && !token)} className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[13px] font-medium disabled:opacity-40">{testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}{t("Test connection")}</button></div>}
        />
      </SettingSection>

      <SettingSection title={t("Enrolled courses")} description={t("Discover all courses available to this token, then link them to existing Canvas/Echo360 libraries by subject code or normalized name.")}>
        <SettingRow title={t("Course discovery & sync")} description={t("Discover enrolled courses, automatically link them to course libraries, and sync all lessons, discussions, files, and grades.")} control={<div className="flex flex-wrap gap-2"><button type="button" onClick={() => void discover()} disabled={discovering} className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[13px] font-medium disabled:opacity-40">{discovering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}{t("Discover courses")}</button><button type="button" onClick={() => void register()} disabled={discovering || !tokenSet} className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--foreground)] px-3.5 py-2 text-[13px] font-medium text-[var(--background)] disabled:opacity-40"><GraduationCap className="h-3.5 w-3.5" />{t("Link all courses")}</button><button type="button" onClick={() => void syncAll()} disabled={syncing || !tokenSet} className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[13px] font-medium disabled:opacity-40">{syncing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}{t("Sync all materials")}</button></div>} />
        {courses.length > 0 && <div className="border-t border-[var(--border)]/50 py-4"><div className="grid gap-2 sm:grid-cols-2">{courses.map((course) => <div key={course.id} className="rounded-lg border border-[var(--border)]/60 px-3 py-2"><div className="text-[13px] font-medium text-[var(--foreground)]">{course.name}</div><div className="mt-0.5 font-mono text-[11px] text-[var(--muted-foreground)]">{course.code || course.id}</div></div>)}</div></div>}
      </SettingSection>

      {(message || error) && <div className={`rounded-xl border px-4 py-3 text-[12.5px] ${error ? "border-red-500/30 bg-red-500/5 text-red-600 dark:text-red-400" : "border-emerald-500/30 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400"}`}>{error || message}</div>}
    </div>
  );
}
