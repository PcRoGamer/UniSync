// UniSync backend runtime
(function (GM_xmlhttpRequest, GM_getValue, GM_setValue, GM_openInTab, setTimeout, setInterval, MutationObserver, fetch, bridgeLifecycle) {
  "use strict";

  // Terminate any ghost intervals from earlier script evaluations in this tab
  try {
    if (typeof window !== "undefined") {
      if (window.__unisync_resolver_timer) { clearInterval(window.__unisync_resolver_timer); window.__unisync_resolver_timer = null; }
      if (window.__unisync_harvest_timer) { clearInterval(window.__unisync_harvest_timer); window.__unisync_harvest_timer = null; }
      if (window.__unisync_bridge_timer) { clearInterval(window.__unisync_bridge_timer); window.__unisync_bridge_timer = null; }
      if (window.__unisync_command_timer) { clearInterval(window.__unisync_command_timer); window.__unisync_command_timer = null; }
    }
  } catch (_) {}

  const BACKEND_URL = "http://127.0.0.1:8000";
  const TAB_ID = (() => { try { const k = "unisync-tab-id"; let v = sessionStorage.getItem(k); if (!v) { v = crypto.randomUUID(); sessionStorage.setItem(k, v); } return v; } catch (_) { return Math.random().toString(36).slice(2); } })();
  // GM_openInTab is injected by the loader (4th positional arg); null when loader version lacks the grant
  const _gmOpenInTab = (typeof GM_openInTab === "function") ? GM_openInTab : null;
  const debugResult = (event, data = {}) => GM_xmlhttpRequest({
    method: "POST", url: BACKEND_URL + "/api/v1/sync/bridge/results",
    headers: {"Content-Type": "application/json"},
    data: JSON.stringify({event, host: location.hostname, ...data}), timeout: 8000
  });
  const collectEchoLinks = (raw) => {
    if (!raw) return [];
    const matches = new Set();
    const text = String(raw).replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/\\u0026/g, "&").replace(/\\\//g, "/");
    const patterns = [
      /https?:\/\/echo360\.net\.au\/(?:lti|media|ui\/player)\/[a-zA-Z0-9\-]+(?:\?[^\s"'<>\\]*)?/gi,
      /(?:https?%3A%2F%2F)?(?:[a-zA-Z0-9\-]+\.)?echo360\.net\.au(?:%2F|\/)(?:lti|media|ui%2Fplayer|ui\/player)(?:%2F|\/)[a-zA-Z0-9\-]+(?:%3F[^\s"'<>\\]*|\?[^\s"'<>\\]*)?/gi,
      /(?:https?:\/\/[^\/\s"'<>\\]+)?\/courses\/\d+\/external_tools\/retrieve\?[^\s"'<>\\]+/gi
    ];
    for (const rx of patterns) {
      let m;
      while ((m = rx.exec(text)) !== null) {
        let u = m[0];
        for (let i = 0; i < 2; i++) {
          try { if (/%[0-9a-f]{2}/i.test(u)) u = decodeURIComponent(u); } catch (_) {}
        }
        const mParam = u.match(/[?&]url=([^&]+)/i);
        if (mParam) {
          try { u = decodeURIComponent(mParam[1]); } catch (_) { u = mParam[1]; }
        }
        if (/echo360\.net\.au\/(?:lti|media|ui\/player)\//i.test(u)) {
          if (u.startsWith("//")) u = "https:" + u;
          else if (!u.startsWith("http")) u = "https://" + u.replace(/^\/+/, "");
          matches.add(u);
        }
      }
    }
    return Array.from(matches);
  };

  async function harvestBlaw() {
    debugResult("blaw-start");
    const pages = new Map();
    let pageApiUrl = "/api/v1/courses/240718/pages?per_page=100";
    while (pageApiUrl && bridgeLifecycle.active()) {
      try {
        const list = await canvasFetch(pageApiUrl);
        const data = await list.json();
        debugResult("blaw-pages", {status: list.status, count: Array.isArray(data) ? data.length : 0});
        if (Array.isArray(data)) data.forEach(p => pages.set(p.url, p));
        pageApiUrl = ((list.headers.get("Link") || "").match(/<([^>]+)>; rel="next"/) || [])[1];
      } catch (_) { break; }
    }
    let next = "/api/v1/courses/240718/modules?per_page=100";
    while (next && bridgeLifecycle.active()) {
      const res = await canvasFetch(next), modules = await res.json();
      debugResult("blaw-modules", {status: res.status, count: Array.isArray(modules) ? modules.length : 0});
      if (!Array.isArray(modules)) break;
      for (const mod of modules) {
        let itemUrl = "/api/v1/courses/240718/modules/" + mod.id + "/items?per_page=100";
        while (itemUrl && bridgeLifecycle.active()) {
          const ir = await canvasFetch(itemUrl), items = await ir.json();
          if (!Array.isArray(items)) break;
          items.forEach(i => {
            if (i.type === "Page" && i.page_url) pages.set(i.page_url, {url:i.page_url,title:i.title});
            else if (i.type === "ExternalTool" && i.external_url && /echo360\.net\.au/i.test(i.external_url)) {
              pages.set(i.external_url, {url:i.external_url,title:i.title,isDirect:true});
            }
          });
          itemUrl = ((ir.headers.get("Link") || "").match(/<([^>]+)>; rel="next"/) || [])[1];
        }
      }
      next = ((res.headers.get("Link") || "").match(/<([^>]+)>; rel="next"/) || [])[1];
    }
    debugResult("blaw-page-inventory", {count: pages.size});
    const candidates = Array.from(pages.values());
    debugResult("blaw-candidates", {count: candidates.length, first: candidates[0] && candidates[0].url});
    for (const page of candidates) {
      if (!page.url || !bridgeLifecycle.active()) continue;
      if (page.isDirect) {
        const directItems = [{
          url: page.url,
          title: page.title || "Echo360 Video",
          page_title: page.title || "Module Item",
          page_url: page.url
        }];
        await new Promise(resolve => GM_xmlhttpRequest({
          method:"POST", url:BACKEND_URL+"/api/v1/sync/payload",
          headers:{"Content-Type":"application/json"},
          data:JSON.stringify({hostname:location.hostname,data:{type:"canvas_courses",courses:[{id:"240718",name:"Principles of Business Law (BLAW10001_2026_SM2)",echo_embeds:directItems}]}}),
          timeout:15000,onload:resolve,onerror:resolve,ontimeout:resolve
        }));
        continue;
      }
      let html = "";
      try {
        const pApi = await canvasFetch("/api/v1/courses/240718/pages/" + encodeURIComponent(page.url));
        const pJson = await pApi.json();
        if (pJson && pJson.body) html += " " + pJson.body;
        if (pJson && pJson.title && !page.title) page.title = pJson.title;
      } catch (_) {}
      try {
        const res = await canvasFetch("/courses/240718/pages/" + encodeURIComponent(page.url), {headers:{Accept:"text/html"}});
        html += " " + (await res.text());
      } catch (_) {}

      const links = collectEchoLinks(html);
      debugResult("blaw-page", {page:page.url,title:page.title,links:links.length});
      if (links.length) {
        const richEmbeds = links.map(u => ({
          url: u,
          title: page.title || page.url,
          page_title: page.title || page.url,
          page_url: page.url
        }));
        await new Promise(resolve => GM_xmlhttpRequest({
          method:"POST", url:BACKEND_URL+"/api/v1/sync/payload",
          headers:{"Content-Type":"application/json"},
          data:JSON.stringify({hostname:location.hostname,data:{type:"canvas_courses",courses:[{id:"240718",name:"Principles of Business Law (BLAW10001_2026_SM2)",echo_embeds:richEmbeds}]}}),
          timeout:15000,onload:r=>{debugResult("blaw-persist",{page:page.url,title:page.title,status:r.status,count:links.length});resolve();},onerror:resolve,ontimeout:resolve
        }));
      }
    }
    debugResult("blaw-complete");
    setTimeout(() => syncEcho360().catch(e => debugResult("blaw-resolve-error", {message:String(e)})), 500);
  }

  // ── Autonomous Resolver Coordinator ─────────────────────────────────────────
  // Sequentially opens unresolved LTI launch targets in active tabs, coordinates
  // provenance via GM_setValue, extracts player-properties, and self-closes tabs.
  let autoResolverRunning = false;
  async function runAutoResolver() {
    if (autoResolverRunning || location.hostname !== "canvas.lms.unimelb.edu.au" || !bridgeLifecycle.active()) return;
    if (typeof _gmOpenInTab !== "function") {
      debugResult("auto-resolver-skip", { reason: "no_gm_open_in_tab" });
      return;
    }

    const now = Date.now();

    // 1. Leader election: ensure only ONE Canvas tab coordinates launches
    try {
      const leaderRaw = GM_getValue("unisync_resolver_leader", "null");
      let leader = null;
      if (leaderRaw && leaderRaw !== "null") leader = JSON.parse(leaderRaw);
      if (leader && leader.tabId !== TAB_ID && (now - (leader.heartbeat || 0) < 25000)) {
        return; // Sibling Canvas tab is the active coordinator
      }
      GM_setValue("unisync_resolver_leader", JSON.stringify({ tabId: TAB_ID, heartbeat: now }));
    } catch (_) {}

    // 2. Global in-flight lock: wait for prior target to download & self-close
    try {
      const activeRaw = GM_getValue("unisync_resolving_target", "null");
      if (activeRaw && activeRaw !== "null") {
        const active = JSON.parse(activeRaw);
        if (now - (active.started_at || 0) < 120000) {
          return; // Still waiting for prior target to resolve, download, and self-close
        }
        GM_setValue("unisync_resolving_target", "null");
      }
    } catch (_) {}

    autoResolverRunning = true;
    try {
      const targetsData = await new Promise((resolve) => {
        GM_xmlhttpRequest({
          method: "GET",
          url: BACKEND_URL + "/api/v1/lectures/libraries/echo360-sync-targets",
          timeout: 10000,
          onload: (r) => {
            try { resolve(JSON.parse(r.responseText)); } catch (_) { resolve({}); }
          },
          onerror: () => resolve({}),
          ontimeout: () => resolve({})
        });
      });

      const embedTargets = targetsData.embed_targets || [];
      const unresolved = embedTargets.filter(t => 
        t.canvas_launch_url && 
        !t.downloaded &&
        !t.subtitle_available &&
        !t.media_url
      );

      // 3. Attempt tracking & backoff: skip targets attempted >= 1 time to prevent duplicate loops
      let attempts = {};
      try { attempts = JSON.parse(GM_getValue("unisync_attempt_counts", "{}")); } catch (_) {}
      let activeResolving = {};
      try { activeResolving = JSON.parse(GM_getValue("unisync_active_resolving_ids", "{}")); } catch (_) {}

      for (const k of Object.keys(activeResolving)) {
        if (now - activeResolving[k] > 120000) delete activeResolving[k];
      }
      GM_setValue("unisync_active_resolving_ids", JSON.stringify(activeResolving));

      // Do not wipe attempts in an aggressive 5-minute loop. Keep attempted
      // targets suppressed for 24 hours so browser tabs are not repeatedly opened.
      let attemptTimes = {};
      try { attemptTimes = JSON.parse(GM_getValue("unisync_attempt_times", "{}")); } catch (_) {}
      for (const k of Object.keys(attempts)) {
        if (attemptTimes[k] && (now - attemptTimes[k] > 86400000)) {
          delete attempts[k];
          delete attemptTimes[k];
        }
      }
      GM_setValue("unisync_attempt_counts", JSON.stringify(attempts));
      GM_setValue("unisync_attempt_times", JSON.stringify(attemptTimes));

      const candidates = unresolved.filter(t => {
        const key = t.link_id ? (t.uuid + "_" + t.link_id) : (t.uuid || t.media_id);
        if (activeResolving[key]) return false;
        // Strictly allow only 1 attempt per target to prevent tab-opening loops
        if ((attempts[key] || 0) >= 1) return false;
        return true;
      });

      candidates.sort((a, b) => {
        const aMissing = (!a.media_url ? 2 : 0) + (!a.subtitle_available ? 2 : 0);
        const bMissing = (!b.media_url ? 2 : 0) + (!b.subtitle_available ? 2 : 0);
        return bMissing - aMissing;
      });

      debugResult("auto-resolver-status", { totalEmbeds: embedTargets.length, pending: unresolved.length, actionable: candidates.length });

      if (candidates.length > 0) {
        const target = candidates[0];
        const targetKey = target.link_id ? (target.uuid + "_" + target.link_id) : (target.uuid || target.media_id);
        const launchUrl = target.canvas_launch_url + (target.canvas_launch_url.includes("?") ? "&" : "?") + "unisync_auto=1&unisync_lid=" + encodeURIComponent(target.link_id || "");

        attempts[targetKey] = (attempts[targetKey] || 0) + 1;
        attemptTimes[targetKey] = now;
        activeResolving[targetKey] = now;
        GM_setValue("unisync_attempt_counts", JSON.stringify(attempts));
        GM_setValue("unisync_attempt_times", JSON.stringify(attemptTimes));
        GM_setValue("unisync_active_resolving_ids", JSON.stringify(activeResolving));

        debugResult("auto-resolver-launch", { title: target.title, linkId: target.link_id, targetKey, attempt: attempts[targetKey], url: launchUrl.slice(0, 120) });

        GM_setValue("unisync_resolving_target", JSON.stringify({
          link_id: target.link_id,
          uuid: target.uuid,
          target_key: targetKey,
          title: target.title,
          started_at: now
        }));

        // Open in background tab without stealing active focus
        const tabRef = _gmOpenInTab(launchUrl, { active: false, insert: true });
        // Adaptive tab lifecycle: poll resolving target status and close tab as soon as
        // child Echo360 tab finishes its scrape (or safely after 45s if stalled).
        // A rigid short timer (e.g. 12s) prematurely killed tabs while Echo360 was
        // still executing its LTI redirect chain and downloading player properties.
        if (tabRef && typeof tabRef.close === "function") {
          const pollStart = Date.now();
          const closePoll = setInterval(() => {
            let done = false;
            try {
              const cur = GM_getValue("unisync_resolving_target", "null");
              if (!cur || cur === "null") done = true;
            } catch (_) {}
            const elapsed = Date.now() - pollStart;
            if (done || elapsed > 45000) {
              clearInterval(closePoll);
              try {
                if (!tabRef.closed) tabRef.close();
              } catch (_) {}
            }
          }, 1000);
        }
      }
    } catch (err) {
      debugResult("auto-resolver-error", { message: String(err) });
    } finally {
      autoResolverRunning = false;
    }
  }

  const SCRIPT_REVISION = "blaw-auto-32";
  try {
    if (GM_getValue("unisync_runtime_revision") !== SCRIPT_REVISION) {
      GM_setValue("unisync_runtime_revision", SCRIPT_REVISION);
      GM_setValue("unisync_attempt_counts", "{}");
      GM_setValue("unisync_attempt_times", "{}");
      GM_setValue("unisync_active_resolving_ids", "{}");
      GM_setValue("unisync_resolving_target", "null");
      debugResult("runtime-revision-migrated", { revision: SCRIPT_REVISION });
    }
  } catch (_) {}

  debugResult("runtime-loaded", {revision: SCRIPT_REVISION});
  let lastBlawHarvestAt = 0;
  const runBlawHarvest = () => {
    if (location.hostname !== "canvas.lms.unimelb.edu.au" || Date.now() - lastBlawHarvestAt < 300000) return;
    lastBlawHarvestAt = Date.now();
    harvestBlaw().catch(e => debugResult("blaw-error", {message:String(e)}));
  };
  if (location.hostname === "canvas.lms.unimelb.edu.au") {
    setTimeout(runBlawHarvest, 2000);
    window.__unisync_harvest_timer = setInterval(runBlawHarvest, 300000);
    setTimeout(runAutoResolver, 8000);
    window.__unisync_resolver_timer = setInterval(runAutoResolver, 60000);
  }

  // P1-7: guard — skip if previous sync still in-flight
  let hybridInFlight = false;

  // P2-17: dedup — minimum interval between hybridSync runs to prevent
  //         setTimeout + setInterval overlap from hammering the backend
  let lastRunAt = 0;
  const MIN_INTERVAL_MS = 10000;

  // P1-8: delta — persist seen lesson_ids across invocations via GM storage
  let lastSyncedIds;
  try {
    lastSyncedIds = new Set(JSON.parse(GM_getValue("dt_lastIds", "[]") || "[]"));
  } catch (_e) {
    lastSyncedIds = new Set();
  }
  function persistLastIds() {
    try {
      if (lastSyncedIds.size > 2000) {
        const toKeep = Array.from(lastSyncedIds).slice(-1500);
        lastSyncedIds = new Set(toKeep);
      }
      const arr = Array.from(lastSyncedIds);
      GM_setValue("dt_lastIds", JSON.stringify(arr));
    } catch (_e) { /* GM_setValue unavailable – in-memory only */ }
  }

  /**
   * Universal fetch wrapper for Canvas LMS using GM_xmlhttpRequest.
   * Enables querying Canvas REST APIs with active browser session cookies from
   * ANY tab (including localhost:3000) without requiring the user to navigate to Canvas.
   */
  const canvasFetch = (path, options = {}) => {
      const url = path.startsWith("http")
          ? path
          : `https://canvas.lms.unimelb.edu.au${path.startsWith("/") ? "" : "/"}${path}`;
      return new Promise((resolve, reject) => {
          GM_xmlhttpRequest({
              method: options.method || "GET",
              url: url,
              headers: Object.assign({
                  "Accept": "application/json, text/plain, */*",
              }, options.headers || {}),
              responseType: options.responseType || "text",
              timeout: options.timeout || 30000,
              onload: (resp) => {
                  resolve({
                      ok: resp.status >= 200 && resp.status < 300,
                      status: resp.status,
                      headers: {
                          get: (h) => resp.responseHeaders ? (resp.responseHeaders.match(new RegExp(h + ":\\s*([^\\r\\n]+)", "i")) || [])[1] || "" : ""
                      },
                      json: async () => {
                          try { return JSON.parse(resp.responseText); } catch (_e) { return null; }
                      },
                      text: async () => resp.responseText,
                      blob: async () => resp.response,
                      response: resp.response,
                  });
              },
              onerror: (err) => reject(new Error(err.error || "Canvas network error")),
              ontimeout: () => reject(new Error("Canvas network timeout")),
          });
      });
  };

  /**
   * Extract real Echo360 lesson/media ids from player-page HTML.
   * Public and secure player pages embed a STRING-ESCAPED bootstrap JSON
   * (\"mediaId\":\"…\") — the backslash escapes defeat normal ["\s]* regex
   * classes, so unescape a copy of the HTML before matching.
   * Returns {} when the page exposes no bootstrap JSON.
   */
  const extractEchoIds = (html) => {
    const ids = {};
    if (!html) return ids;
    try {
      const unescaped = String(html).replace(/\\\\"/g, '"');
      let m = unescaped.match(/"mediaId"\s*:\s*"([a-f0-9\-]{36})"/i)
        || unescaped.match(/mediaId["'\\s]*[:=]["'\\s]*([a-f0-9\-]{36})/i);
      if (m) ids.mediaId = m[1];
      m = unescaped.match(/"sessionId"\s*:\s*"([a-f0-9\-]{36})"/i)
        || unescaped.match(/sessionId["'\\s]*[:=]["'\\s]*([a-f0-9\-]{36})/i);
      if (m) ids.sessionId = m[1];
    } catch (_e) {}
    return ids;
  };

  /**
   * Universal fetch wrapper for Echo360 using GM_xmlhttpRequest.
   * Enables querying Echo360 syllabuses & transcripts with active session cookies
   * from any tab without requiring navigation to echo360.net.au.
   *
   * SAFEGUARD: Detects when Echo360 responds with a redirect or HTML from login.echo360.net.au.
   * UniMelb accounts authenticate exclusively via Canvas LTI (external tool 701).
   * Standalone login.echo360.net.au is a dead end and must NEVER be treated as valid data.
   */
  const echoFetch = (path, options = {}) => {
      const url = path.startsWith("http")
          ? path
          : `https://echo360.net.au${path.startsWith("/") ? "" : "/"}${path}`;
      return new Promise((resolve, reject) => {
          GM_xmlhttpRequest({
              method: options.method || "GET",
              url: url,
              headers: Object.assign({
                  "Accept": "application/json, text/plain, */*",
              }, options.headers || {}),
              responseType: options.responseType || "text",
              timeout: options.timeout || 30000,
              onload: (resp) => {
                  const finalUrl = resp.finalUrl || "";
                  const respText = typeof resp.responseText === "string" ? resp.responseText : "";
                  const isLoginPage = finalUrl.includes("login.echo360.net.au") ||
                                      respText.includes("login.echo360.net.au") ||
                                      respText.includes("c08c41ee-50e3-45e8-a6e6-e9579b28f620");
                  const isAuth = !isLoginPage && resp.status >= 200 && resp.status < 300;
                  resolve({
                      ok: isAuth,
                      unauthenticated: isLoginPage || resp.status === 401 || resp.status === 403,
                      status: isLoginPage ? 401 : resp.status,
                      finalUrl: finalUrl,
                      headers: {
                          get: (h) => resp.responseHeaders ? (resp.responseHeaders.match(new RegExp(h + ":\\s*([^\\r\\n]+)", "i")) || [])[1] || "" : ""
                      },
                      json: async () => {
                          if (isLoginPage) return null;
                          try { return JSON.parse(resp.responseText); } catch (_e) { return null; }
                      },
                      text: async () => isLoginPage ? "" : respText,
                      blob: async () => resp.response,
                      response: resp.response,
                  });
              },
              onerror: (err) => reject(new Error(err.error || "Echo360 network error")),
              ontimeout: () => reject(new Error("Echo360 network timeout")),
          });
      });
  };

  let lastLtiHandshake = 0;
  /**
   * Silent background LTI SSO handshake for Canvas tabs.
   * If Echo360 session has expired, opens a hidden iframe to Canvas tool 4090 to refresh session cookies.
   */
  function triggerLtiHandshake(customUrl) {
    if (Date.now() - lastLtiHandshake < 120000) return;
    lastLtiHandshake = Date.now();
    if (typeof window !== "undefined" && window.location && window.location.hostname.includes("canvas.lms.unimelb.edu.au")) {
      console.log("[UniSync] Performing background Echo360 LTI SSO handshake via Canvas...");
      try {
        const launchUrl = customUrl || GM_getValue("last_known_lti_url") || "https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/701";
        const ifr = document.createElement("iframe");
        ifr.style.position = "absolute";
        ifr.style.left = "-9999px";
        ifr.style.top = "-9999px";
        ifr.style.width = "1px";
        ifr.style.height = "1px";
        ifr.style.opacity = "0";
        ifr.src = launchUrl;
        document.body.appendChild(ifr);
        setTimeout(() => {
          try { ifr.remove(); } catch (_e) {}
        }, 10000);
      } catch (_e) {}
    }
  }

  /**
   * Resolves the actual installed userscript loader version from the host context.
   * Checks bridgeLifecycle.version, then GM_getValue("unisync_loader_version"),
   * falling back to canonical "4.0.3".
   */
  const getLoaderVersion = () => {
    try {
      if (typeof bridgeLifecycle !== "undefined" && bridgeLifecycle && bridgeLifecycle.version) {
        return String(bridgeLifecycle.version).trim();
      }
      if (typeof GM_getValue === "function") {
        const stored = GM_getValue("unisync_loader_version");
        if (stored) return String(stored).trim();
      }
    } catch (_e) {}
    return "4.0.3";
  };

  /**
   * Reports userscript bridge presence, current active tab, and university session validity to UniSync backend.
   * platform: "canvas" | "echo360" | "outlook" | "perusall" | "generic" — lets the backend
   *            track Canvas LMS and Echo360 auth independently instead of one clobbering the other.
   */
  const sendHeartbeat = (authStatus, message, platform) => {
      try {
          GM_xmlhttpRequest({
              method: "POST",
              url: BACKEND_URL + "/api/v1/sync/bridge/heartbeat",
              headers: { "Content-Type": "application/json" },
              data: JSON.stringify({
                  version: getLoaderVersion(),
                  hostname: window.location.hostname,
                  tab: window.location.href,
                  tab_id: TAB_ID,
                  title: (document.title || "").slice(0, 200),
                  auth_status: authStatus || "authenticated",
                  message: message || "",
                  platform: platform || "generic"
              }),
              timeout: 8000,
              onload: () => {},
              onerror: () => {}
          });
      } catch (_e) {}
  };

  const reportEchoDiagnostic = (message) => {
    console.log(`[UniSync] ${message}`);
    sendHeartbeat("authenticated", `Echo360 diagnostic: ${message}`, "echo360");
  };

  let echoSyncInFlight = false;
  let canvasSyncInFlight = false;
  let lastEchoRun = 0;
  let lastCanvasRun = 0;
  let canvasEmbedObserverStarted = false;
  let lastCanvasEmbedSignature = "";

  // Canvas mounts Echo360 as an iframe after its page shell loads. Persist the
  // actual player URL immediately instead of waiting for the broader Canvas
  // crawl, which only sees an empty API body for pages such as “How PBL Works”.
  function watchCanvasEmbedMounts() {
    if (canvasEmbedObserverStarted || !window.location.hostname.includes("canvas.lms.unimelb.edu.au")) return;
    canvasEmbedObserverStarted = true;
    let pending = null;
    const publish = () => {
      pending = null;
      const courseMatch = window.location.pathname.match(/\/courses\/(\d+)/);
      if (!courseMatch) return;
      const urls = Array.from(document.querySelectorAll('iframe[src*="echo360.net.au"], a[href*="echo360.net.au"], [data-url*="echo360.net.au"], [data-src*="echo360.net.au"]'))
        .map((element) => element.src || element.href || element.getAttribute("data-url") || element.getAttribute("data-src"))
        .filter(Boolean);
      const signature = urls.slice().sort().join("|");
      if (!signature || signature === lastCanvasEmbedSignature) return;
      lastCanvasEmbedSignature = signature;
      GM_xmlhttpRequest({
        method: "POST",
        url: BACKEND_URL + "/api/v1/sync/payload",
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({
          url: window.location.href,
          hostname: "canvas.lms.unimelb.edu.au",
          data: { type: "canvas_courses", courses: [{ id: courseMatch[1], name: document.title || `Course ${courseMatch[1]}`, echo_embeds: urls }] },
        }),
        onload: (response) => console.log(`[UniSync] Canvas iframe embeds persisted — HTTP ${response.status}`),
        onerror: () => console.warn("[UniSync] Canvas iframe embed persistence failed"),
      });
    };
    const schedule = () => {
      if (pending) clearTimeout(pending);
      pending = setTimeout(publish, 600);
    };
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "href", "data-url", "data-src"] });
    schedule();
  }

  /**
   * Autonomous Echo360 Syllabus & Lecture Resolution.
   * Runs independently and immediately when on echo360.net.au or Canvas.
   */
  async function syncEcho360() {
    if (echoSyncInFlight || Date.now() - lastEchoRun < 8000) return;
    echoSyncInFlight = true;
    lastEchoRun = Date.now();

    try {
      const targetData = await new Promise((resolve) => {
        GM_xmlhttpRequest({
          method: "GET",
          url: BACKEND_URL + "/api/v1/lectures/libraries/echo360-sync-targets",
          timeout: 10000,
          onload: (res) => {
            try { resolve(JSON.parse(res.responseText)); } catch (_e) { resolve({ targets: [], _fetchError: "bad-json" }); }
          },
          onerror: (err) => resolve({ targets: [], _fetchError: String((err && err.error) || "error") }),
          ontimeout: () => resolve({ targets: [], _fetchError: "timeout" }),
        });
      });

      const targets = targetData.targets || [];
      const defaultLtiUrl = targetData.lti_launch_url || GM_getValue("last_known_lti_url") || "https://canvas.lms.unimelb.edu.au/courses/241246/external_tools/701";
      if (!targets.length) {
        console.warn("[UniSync] No Echo360 targets — backend " + BACKEND_URL + " unreachable? (" + (targetData._fetchError || "empty-targets") + ")");
        return;
      }
      console.log("[UniSync] Echo360 sync: " + targets.length + " section target(s), " + (targetData.embed_targets || []).length + " embed target(s), " + (targetData.ingested_echo360_ids || []).length + " already ingested (skipped)");
      const ingestedIds = new Set(targetData.ingested_echo360_ids || []);

      const sections = [];
      let handshakeTriggered = false;
      let sessionConfirmed = false;

      for (const target of targets) {
        if (!target.course_id) continue;
        const targetLti = target.lecture_capture_url 
          || (target.canvas_course_id && String(target.canvas_course_id).match(/^\d+$/)
              ? "https://canvas.lms.unimelb.edu.au/courses/" + target.canvas_course_id + "/external_tools/701"
              : defaultLtiUrl);

        try {
          const sylRes = await echoFetch("/section/" + target.course_id + "/syllabus");
          if (sylRes.unauthenticated || sylRes.status === 401 || sylRes.status === 403) {
            // Once authenticated via ANY course, Echo360 session cookie unlocks ALL playlists.
            // Trigger handshake once if session is unauthenticated.
            if (!handshakeTriggered) {
              handshakeTriggered = true;
              sendHeartbeat("waiting_for_auth", "Echo360 session not authenticated. Launch via Canvas Lecture Capture: " + targetLti, "echo360");
              triggerLtiHandshake(targetLti);
            }
            continue;
          }
          if (!sylRes.ok) throw new Error("Syllabus HTTP " + sylRes.status);

          const sylData = await sylRes.json();
          if (!sylData || !sylData.data) {
            if (!handshakeTriggered) {
              handshakeTriggered = true;
              sendHeartbeat("waiting_for_auth", "Echo360 session not authenticated. Launch via Canvas Lecture Capture: " + targetLti, "echo360");
              triggerLtiHandshake(targetLti);
            }
            continue;
          }

          // Confirmed: single Echo360 session is active and unlocks all playlists
          sessionConfirmed = true;
          sendHeartbeat("authenticated", "Connected to Echo360: " + (target.library_name || target.course_id), "echo360");
          const syllabus = Array.isArray(sylData.data) ? sylData.data : (sylData.data.syllabus || sylData.data.items || []);
          if (!Array.isArray(syllabus)) throw new Error("Unexpected syllabus response");
          sendHeartbeat("authenticated", "Echo360 import: " + target.library_name + " — " + syllabus.length + " classes", "echo360");

          // Only process lessons that have media or content
          const mediaLessons = syllabus.filter(item => {
            const lesObj = item.lesson || {};
            const lesson = lesObj.lesson || lesObj;
            const medias = lesObj.medias || item.medias || [];
            return Boolean(lesObj.hasMedia || lesson.hasMedia || item.hasMedia || medias.length > 0);
          });

          // Fetch subtitles and media URLs with concurrency limit of 3
          for (let i = 0; i < mediaLessons.length; i += 3) {
            const chunk = mediaLessons.slice(i, i + 3);
            await Promise.allSettled(chunk.map(async (item) => {
              const lesObj = item.lesson || {};
              const medias = lesObj.medias || item.medias || [];
              const lesson = lesObj.lesson || lesObj;
              const lessonId = lesson?.id;
              const mediaId = medias[0]?.id;
              if (!lessonId) return;
              if (ingestedIds.has(lessonId)) return; // already ingested — keep syllabus shell, skip refetch

              // 1. Subtitles / Transcripts
              let subText = null;
              let subJson = null;
              try {
                let res = await echoFetch(`/media/download/${lessonId}/transcript.vtt?lessonId=${lessonId}`, { timeout: 12000 });
                if (!res.ok && mediaId) res = await echoFetch(`/media/download/${mediaId}/transcript.vtt?lessonId=${lessonId}`, { timeout: 12000 });
                if (!res.ok && mediaId) res = await echoFetch(`/api/ui/echoplayer/lessons/${lessonId}/medias/${mediaId}/transcript-file?format=vtt`, { timeout: 12000 });
                if (res.ok) {
                  const text = await res.text();
                  if (text.trim().startsWith("WEBVTT")) subText = text;
                }
              } catch (e) {}

              if (!subText) {
                try {
                  const jsonRes = await echoFetch(`/api/ui/echoplayer/lessons/${lessonId}/transcript`, { timeout: 12000 });
                  if (jsonRes.ok) subJson = await jsonRes.json();
                } catch (e) {}
              }

              if (subText) item.subtitles = subText;
              else if (subJson) item.subtitles_json = subJson;

              // 2. Video / Audio stream resolution
              let resolvedMedia = null;
              const jsonEndpoints = [
                `/api/ui/echoplayer/lessons/${encodeURIComponent(lessonId)}/medias/${encodeURIComponent(mediaId)}/player-properties`,
                `/api/ui/echoplayer/lessons/${encodeURIComponent(lessonId)}/medias/${encodeURIComponent(mediaId)}`,
                `/api/ui/echoplayer/lessons/${encodeURIComponent(lessonId)}`,
              ];

              for (const ep of jsonEndpoints) {
                if (resolvedMedia) break;
                try {
                  const res = await echoFetch(ep, { timeout: 12000 });
                  if (!res.ok) continue;
                  const ct = res.headers.get("content-type") || "";
                  if (ct.includes("json")) {
                    const json = await res.json();
                    const jsonStr = JSON.stringify(json);
                    const m3u8 = jsonStr.match(/https?:\/\/[^"\\]+\.m3u8[^"\\]*/i);
                    if (m3u8) {
                      resolvedMedia = { media_url: m3u8[0], media_kind: "m3u8" };
                      break;
                    }
                    const mp4 = jsonStr.match(/https?:\/\/[^"\\]+\.mp4[^"\\]*/i);
                    if (mp4) {
                      resolvedMedia = { media_url: mp4[0], media_kind: "mp4" };
                      break;
                    }
                  } else {
                    const text = await res.text();
                    const m3u8 = text.match(/https?:\/\/[^"'\s\\]+\.m3u8[^"'\s\\]*/i);
                    if (m3u8) {
                      resolvedMedia = { media_url: m3u8[0].replace(/\\u0026/g, "&"), media_kind: "m3u8" };
                      break;
                    }
                    const mp4 = text.match(/https?:\/\/[^"'\s\\]+\.mp4[^"'\s\\]*/i);
                    if (mp4) {
                      resolvedMedia = { media_url: mp4[0].replace(/\\u0026/g, "&"), media_kind: "mp4" };
                      break;
                    }
                  }
                } catch (_e) {}
              }

              if (!resolvedMedia && mediaId) {
                try {
                  const pubRes = await echoFetch(`/media/${encodeURIComponent(mediaId)}/public`, { timeout: 12000 });
                  if (pubRes.ok) {
                    const html = await pubRes.text();
                    const m3u8 = html.match(/https?:\/\/[^"'\s\\]+\.m3u8[^"'\s\\]*/i);
                    if (m3u8) resolvedMedia = { media_url: m3u8[0].replace(/\\u0026/g, "&"), media_kind: "m3u8" };
                    else {
                      const mp4 = html.match(/https?:\/\/[^"'\s\\]+\.mp4[^"'\s\\]*/i);
                      if (mp4) resolvedMedia = { media_url: mp4[0].replace(/\\u0026/g, "&"), media_kind: "mp4" };
                    }
                  }
                } catch (_e) {}
              }

              if (resolvedMedia) {
                item.media_url = resolvedMedia.media_url;
                item.media_kind = resolvedMedia.media_kind;
              }

              // 3. Thumbnail harvesting. The S3 `thumbnailUri` often returns 403
              //    cross-domain, so we FIRST try to get a poster from the same
              //    authenticated Echo360 player-properties endpoint that yields the
              //    media_url (reachable with the session cookie), then fall back to
              //    the S3 URL. Each candidate is fetched as a blob and verified.
              const mediaObj = (medias && medias[0]) || {};
              const rawThumb = mediaObj.thumbnailUri || item.thumbnailUri;
              const posterSrcs = [];
              // a) player-properties JSON poster (echo360.net.au — session-covered)
              try {
                const pp = await echoFetch(`/api/ui/echoplayer/lessons/${encodeURIComponent(lessonId)}/medias/${encodeURIComponent(mediaId || "")}/player-properties`, { timeout: 12000 });
                if (pp.ok) {
                  const ppJsonText = await pp.text();
                  // Extract any image-looking URL from player-properties (poster/thumbnail/og:image).
                  const posterHits = [];
                  const imgRe = /https?:\/\/[^\s"'\\]+\.(?:jpg|jpeg|png)(?:\?[^\s"'\\]*)?/gi;
                  let m;
                  while ((m = imgRe.exec(ppJsonText)) !== null) {
                    posterHits.push(m[0]);
                  }
                  for (const u of posterHits) {
                    const clean = u.replace(/\\u0026/g, "&").replace(/\\\//g, "/");
                    if (posterSrcs.indexOf(clean) === -1) posterSrcs.push(clean);
                  }
                }
              } catch (_ppErr) {}
              // b) public page og:image (echo360.net.au — session-covered)
              try {
                if (mediaId) {
                  const pubRes = await echoFetch(`/media/${encodeURIComponent(mediaId)}/public`, { timeout: 12000 });
                  if (pubRes.ok) {
                    const pubText = await pubRes.text();
                    const imgRe2 = /https?:\/\/[^\s"'\\]+\.(?:jpg|jpeg|png)(?:\?[^\s"'\\]*)?/gi;
                    let m2;
                    while ((m2 = imgRe2.exec(pubText)) !== null) {
                      const clean = m2[0].replace(/\\u0026/g, "&").replace(/\\\//g, "/");
                      if (posterSrcs.indexOf(clean) === -1) posterSrcs.push(clean);
                    }
                  }
                }
              } catch (_pubErr) {}
              // ensure the S3 thumbnailUri is tried last (sometimes it does work)
                            if (rawThumb && posterSrcs.indexOf(rawThumb) === -1) posterSrcs.push(rawThumb);

                            // c) Host-rewritten thumbnail CDN. The S3 `thumbnailUri` is private
                            //    and 403s cross-domain, but `thumbnails.echo360.net.au` is the
                            //    SAME-SITE thumbnail host (session cookie attaches) that serves
                            //    the same poster path. Prefer it over the S3 URL.
                            if (rawThumb) {
                              try {
                                const rew = rawThumb
                                  .replace(/^https?:\/\/(?:[a-z0-9\-]+\.)*s3\.amazonaws\.com/i, "https://thumbnails.echo360.net.au")
                                  .replace(/^https?:\/\/[a-z0-9\-]*\.?echo360\.net\.au/iu, "https://thumbnails.echo360.net.au");
                                if (rew && rew !== rawThumb) {
                                  posterSrcs.unshift(rew);
                                }
                              } catch (_rwErr) {}
                            }

              let b64 = null;
              let posterUsed = "";
              for (const cand of posterSrcs) {
                if (b64) break;
                try {
                  const r = await echoFetch(cand, { responseType: "blob", timeout: 12000 });
                  if (r.ok && r.blob) {
                    const blob = await r.blob();
                    if (blob && blob.size > 50) {
                      const reader = new FileReader();
                      const bP = new Promise((res) => { reader.onloadend = () => res(reader.result); reader.onerror = () => res(null); });
                      reader.readAsDataURL(blob);
                      const b = await bP;
                      if (b && typeof b === "string" && b.length > 50) { b64 = b; posterUsed = cand; }
                    }
                  }
                } catch (_e2) {}
              }

              if (b64 && !item.thumbnail_b64) {
                item.thumbnail_b64 = b64;
                // Awaited, verified ingest so a failed POST is not silent.
                // Retries each pass until the backend confirms it saved.
                try {
                  const ingestRes = await new Promise((resolve) => {
                    GM_xmlhttpRequest({
                      method: "POST",
                      url: BACKEND_URL + "/api/v1/lectures/thumbnails/ingest",
                      headers: { "Content-Type": "application/json" },
                      data: JSON.stringify({
                        thumbnails: [{
                          lesson_id: lessonId,
                          media_id: mediaId || null,
                          image_b64: b64,
                        }]
                      }),
                      timeout: 15000,
                      onload: (r) => resolve({ ok: r.status >= 200 && r.status < 300, text: r.responseText || "" }),
                      onerror: () => resolve({ ok: false, text: "" }),
                      ontimeout: () => resolve({ ok: false, text: "timeout" }),
                    });
                  });
                  const saved = ingestRes.ok && /"saved_count"\s*:\s*[1-9]/.test(ingestRes.text);
                  if (!saved) {
                    // Keep thumbnail_b64 blank so the next pass retries.
                    delete item.thumbnail_b64;
                  } else if (saved) {
                    sendHeartbeat("authenticated", "Echo360 thumbnail ingested (" + (posterUsed.indexOf("echo360.net.au") !== -1 ? "api" : "s3") + "): " + lessonId, "echo360");
                  }
                } catch (_igErr) {
                  delete item.thumbnail_b64; // retry next pass
                }
              } else if (!b64) {
                // Diagnostic: report what the player-properties poster hunt found so
                // we can tell why the poster isn't landing.
                sendHeartbeat("authenticated", "Thumb harvest miss: " + lessonId + " (" + (posterSrcs.length) + " candidates)", "echo360");
              }
            }));
          }

          sections.push({ sectionId: target.course_id, syllabus });
        } catch (_targetErr) {
          sendHeartbeat("error", "Echo360 import: " + target.library_name + " — " + String(_targetErr), "echo360");
        }
      }

      // 2. Autonomous Canvas & Standalone Echo360 Embed Targets (e.g. BLAW10001)
      const embedTargets = targetData.embed_targets || [];
      debugResult("blaw-embed-block", { count: embedTargets.length });
      if (embedTargets.length > 0) {
        const embedLessons = [];
        for (let i = 0; i < embedTargets.length; i += 10) {
          const chunk = embedTargets.slice(i, i + 10);
          await Promise.allSettled(chunk.map(async (em) => {
            const uuid = em.uuid;
            // LTI deep links: same tool id, distinct video per linkId.
            let linkId = em.link_id || null;
            if (!linkId && em.player_url) {
              const mL = em.player_url.match(/linkId=([a-f0-9\-]+)/i);
              if (mL) linkId = mL[1];
            }
            const embedKey = linkId ? (uuid + "_" + linkId.slice(0, 8)) : uuid;
            debugResult("blaw-embed-start", { uuid: String(uuid), key: embedKey, ingested: ingestedIds.has(`BLAW_EMBED_${embedKey}`) });
            // Already ingested — keep a lightweight shell so the catalog
            // entry survives the wholesale syllabus overwrite, skip refetch.
            // Re-resolve embeds until the catalog has confirmed a stream or
            // subtitle. A prior shell record must not suppress acquisition.
            if (ingestedIds.has(`BLAW_EMBED_${embedKey}`) && (em.media_url || em.subtitle_available)) {
              embedLessons.push({
                secId: em.section_id || "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
                secName: em.course_name || "Principles of Business Law (BLAW10001_2026_SM2)",
                secKb: em.kb_name || "BLAW10001_2026_SM2",
                lesson: { lesson: { id: `BLAW_EMBED_${embedKey}`, name: em.title || "Embedded Video", hasMedia: false, timing: {} }, medias: em.media_id ? [{ id: em.media_id }] : [] },
                subtitles: null,
                subtitles_json: null,
                media_url: em.media_url || null,
                media_kind: em.media_kind || "mp4",
              });
              return;
            }
            let mediaId = em.media_id || null;
            let sessionId = em.session_id || null;
            let title = em.title || "Embedded Video";

            if (!sessionId && em.player_url) {
              const m = em.player_url.match(/sessionId=([a-f0-9\-]{36})/i);
              if (m) sessionId = m[1];
            }

            // Always try the public page — for standalone media embeds the lesson
            // (session) id differs from the media uuid, and player-properties needs
            // the real sessionId. The public page HTML itself often embeds the
            // stream (m3u8/mp4) and poster URLs, so extract those directly too.
            let pageStream = null;
            let pagePoster = null;
            // Keep the player-page VTT result.  This must be declared before
            // reading the UI player HTML: Canvas secure-link pages can expose
            // the caption URL there even when the API transcript endpoints do
            // not accept the synthetic lesson/media ids.
            let subText = null;
            let subJson = null;
            let ppStatus = 0;
            if (true) {
              try {
                const pubRes = await echoFetch("/media/" + encodeURIComponent(uuid) + "/public", { timeout: 12000 });
                if (pubRes.ok && !pubRes.unauthenticated) {
                  const html = await pubRes.text();
                  const mTitle = html.match(/<title>(.*?)<\/title>/i);
                  if (mTitle && mTitle[1]) title = mTitle[1].trim();
                  // The public page carries ids in a STRING-ESCAPED bootstrap JSON
                  // (\"mediaId\":\"…\"). Unescape first or the regexes below never match.
                  const pubIds = extractEchoIds(html);
                  // The bootstrap JSON is ground truth for these embeds: catalog
                  // media_id may hold the public-link uuid, which is NOT the media id.
                  const mMedia = pubIds.mediaId || ((html.match(/mediaId["\\s]*[:=]["\\s]*([a-f0-9\\-]{36})/i) || [])[1] || null);
                  const mSess = pubIds.sessionId || ((html.match(/sessionId["\\s]*[:=]["\\s]*([a-f0-9\\-]{36})/i) || [])[1] || null);
                  if (mMedia) mediaId = mMedia;
                  if (mSess) sessionId = mSess;
                  const mStream = html.match(/https?:\/\/[^"'\\\s]+\.m3u8[^"'\\\s]*/i) || html.match(/https?:\/\/[^"'\\\s]+\.mp4[^"'\\\s]*/i);
                  if (mStream) pageStream = { media_url: mStream[0].replace(/\\u0026/g, "&"), media_kind: /\.m3u8/i.test(mStream[0]) ? "m3u8" : "mp4" };
                  const mPoster = html.match(/https?:\/\/[^"'\\\s]*thumbnails\.echo360\.net\.au[^"'\\\s]*/i) || html.match(/https?:\/\/[^"'\\\s]+\.(?:jpg|jpeg|png)(?:\?[^"'\\\s]*)?/i);
                  if (mPoster) pagePoster = mPoster[0].replace(/\\u0026/g, "&");
                }
              } catch (_e) {}
            }

            // LTI deep link: the linkId selects the video — fetch with it.
            const ltiPath = "/lti/" + encodeURIComponent(uuid) + (linkId ? ("?linkId=" + encodeURIComponent(linkId)) : "");
            if ((!mediaId && !sessionId) || linkId) {
              try {
                const ltiRes = await echoFetch(ltiPath, { timeout: 12000 });
                if (ltiRes.ok && !ltiRes.unauthenticated) {
                  const html = await ltiRes.text();
                  const ltiIds = extractEchoIds(html);
                  const mMedia = ltiIds.mediaId || ((html.match(/mediaId["\\s]*[:=]["\\s]*([a-f0-9\\-]{36})/i) || [])[1] || null);
                  const mSess = ltiIds.sessionId || ((html.match(/sessionId["\\s]*[:=]["\\s]*([a-f0-9\\-]{36})/i) || [])[1] || null);
                  if (mMedia) mediaId = mMedia;
                  if (mSess) sessionId = mSess;
                }
              } catch (_e) {}
            }

            let lessonId = sessionId || uuid;
            let targetMediaId = mediaId || uuid;
            let secureAccessId = null;
            try { secureAccessId = new URL(em.player_url || "", "https://echo360.net.au").searchParams.get("secureLinkAccessDataId"); } catch (_secureUrl) {}
            // Tab-context fallback: the open tab may sit on an Echo player for
            // THIS uuid while the catalog record lacks the secure link id
            // (Canvas sometimes only persists the bare ui/player URL).
            if (!secureAccessId && /echo360\.net\.au$/i.test(window.location.hostname)) {
              try {
                const tU = window.location.href.match(/ui\/player\/([a-f0-9-]{36})/i);
                const tS = window.location.href.match(/secureLinkAccessDataId=([a-f0-9-]{36})/i);
                if (tU && tS && tU[1].toLowerCase() === String(uuid).toLowerCase()) secureAccessId = tS[1];
              } catch (_tErr) {}
            }
            const withSecureAccess = (endpoint) => secureAccessId
              ? endpoint + (endpoint.includes("?") ? "&" : "?") + "secureLinkAccessDataId=" + encodeURIComponent(secureAccessId)
              : endpoint;

            // ui/player embeds: fetch the player page itself — its HTML/JSON
            // often carries the stream, session/media ids, or transcript URLs
            // that the public page does not.
            try {
              const pUrl = em.player_url || "";
              const mCur = pUrl.match(/currentMediaId=([a-f0-9\-]+)/i);
              if (mCur && mCur[1] && !mediaId) mediaId = mCur[1];
              if (/ui\/player/i.test(pUrl) || /secureLinkAccessDataId/i.test(pUrl)) {
                let pPath = "/ui/player/" + encodeURIComponent(uuid);
                try {
                  const u = new URL(pUrl.startsWith("http") ? pUrl : ("https://echo360.net.au" + (pUrl.startsWith("/") ? pUrl : "/" + pUrl)));
                  pPath = u.pathname + u.search;
                } catch (_ue) {}
                const plRes = await echoFetch(pPath, { timeout: 12000 });
                if (plRes.ok && !plRes.unauthenticated) {
                  const pHtml = await plRes.text();
                  const plIds = extractEchoIds(pHtml);
                  const pSess = plIds.sessionId ? [null, plIds.sessionId] : (pHtml.match(/sessionId["\s]*[:=]["\s]*([a-f0-9\-]{36})/i) || pHtml.match(/session[_-]?id["\s]*[:=]["\s]*([a-f0-9\-]{36})/i));
                  if (pSess && pSess[1] && !sessionId) sessionId = pSess[1];
                  const pMedia = plIds.mediaId ? [null, plIds.mediaId] : (pHtml.match(/mediaId["\s]*[:=]["\s]*([a-f0-9\-]{36})/i) || pHtml.match(/media[_-]?id["\s]*[:=]["\s]*([a-f0-9\-]{36})/i));
                  if (pMedia && pMedia[1] && !mediaId) mediaId = pMedia[1];
                  if (!pageStream) {
                    const pStream = pHtml.match(/https?:\/\/[^"'\\\s]+\.m3u8[^"'\\\s]*/i) || pHtml.match(/https?:\/\/[^"'\\\s]+\.mp4[^"'\\\s]*/i);
                    if (pStream) pageStream = { media_url: pStream[0].replace(/\\u0026/g, "&"), media_kind: /\.m3u8/i.test(pStream[0]) ? "m3u8" : "mp4" };
                  }
                  if (!pagePoster) {
                    const pPoster = pHtml.match(/https?:\/\/[^"'\\\s]*thumbnails\.echo360\.net\.au[^"'\\\s]*/i) || pHtml.match(/https?:\/\/[^"'\\\s]+\.(?:jpg|jpeg|png)(?:\?[^"'\\\s]*)?/i);
                    if (pPoster) pagePoster = pPoster[0].replace(/\\u0026/g, "&");
                  }
                  if (!subText) {
                    const pVtt = pHtml.match(/https?:\/\/[^"'\\\s]+\.vtt[^"'\\\s]*/i);
                    if (pVtt) {
                      try {
                        const vRes = await echoFetch(pVtt[0].replace(/\\u0026/g, "&"), { timeout: 12000 });
                        if (vRes.ok && !vRes.unauthenticated) {
                          const vText = await vRes.text();
                          if (vText && vText.trim().startsWith("WEBVTT")) subText = vText;
                        }
                      } catch (_ve) {}
                    }
                  }
                }
              }
            } catch (_pe2) {}
            // Re-resolve ids — the player page may have revealed the real ones.
            lessonId = sessionId || uuid;
            targetMediaId = mediaId || uuid;

            // Fetch subtitles. NOTE the plural /medias/ for ALL transcript
            // endpoints (per the Echo player bundle's transcriptService),
            // whereas player-properties uses singular /media/. The context
            // family must match the link type: secureLinkAccessDataId ->
            // secure-link-access-datas, publicLinkId -> public-links.
            const subEndpoints = [
              targetMediaId ? `/api/ui/echoplayer/public-links/${encodeURIComponent(uuid)}/medias/${encodeURIComponent(targetMediaId)}/transcript-file?format=vtt` : null,
              targetMediaId && secureAccessId ? `/api/ui/echoplayer/secure-link-access-datas/${encodeURIComponent(secureAccessId)}/medias/${encodeURIComponent(targetMediaId)}/transcript-file?format=vtt` : null,
              withSecureAccess(`/media/download/${lessonId}/transcript.vtt?lessonId=${lessonId}`),
              targetMediaId ? withSecureAccess(`/media/download/${targetMediaId}/transcript.vtt?lessonId=${lessonId}`) : null,
              targetMediaId ? withSecureAccess(`/media/download/${targetMediaId}/transcript.vtt?lessonId=${targetMediaId}`) : null,
              targetMediaId ? withSecureAccess(`/media/download/${targetMediaId}/transcript.vtt`) : null,
              targetMediaId ? withSecureAccess(`/api/ui/echoplayer/lessons/${lessonId}/medias/${targetMediaId}/transcript-file?format=vtt`) : null,
            ].filter(Boolean);

            for (const ep of subEndpoints) {
              if (subText) break;
              try {
                const res = await echoFetch(ep, { timeout: 12000 });
                console.log(`[UniSync] Subtitle endpoint ${ep.split("?")[0]} — HTTP ${res.status}`);
                if (res.ok && !res.unauthenticated) {
                  const text = await res.text();
                  if (text && text.trim().startsWith("WEBVTT")) {
                    subText = text;
                    break;
                  }
                }
              } catch (_e) {}
            }

            if (!subText) {
              // JSON cues — context-family first (plural /medias/), then legacy.
              try {
                let jsonRes = await echoFetch(`/api/ui/echoplayer/public-links/${encodeURIComponent(uuid)}/medias/${encodeURIComponent(targetMediaId)}/transcript`, { timeout: 12000 });
                if (!jsonRes.ok && targetMediaId && secureAccessId) jsonRes = await echoFetch(`/api/ui/echoplayer/secure-link-access-datas/${encodeURIComponent(secureAccessId)}/medias/${encodeURIComponent(targetMediaId)}/transcript`, { timeout: 12000 });
                if (!jsonRes.ok) jsonRes = await echoFetch(withSecureAccess(`/api/ui/echoplayer/lessons/${lessonId}/medias/${targetMediaId}/transcript`), { timeout: 12000 });
                if (!jsonRes.ok) jsonRes = await echoFetch(withSecureAccess(`/api/ui/echoplayer/lessons/${lessonId}/transcript`), { timeout: 12000 });
                if (jsonRes.ok && !jsonRes.unauthenticated) {
                  const jData = await jsonRes.json();
                  if (jData && (jData.data || jData.cues || jData.status === "ok")) {
                    subJson = jData;
                  }
                }
              } catch (_e) {}
            }

            // Fetch video stream. The player bundle maps link identity to an
            // echoplayer context type: secureLinkAccessDataId ->
            // secure-link-access-datas, publicLinkId (any /media/<uuid>/public
            // embed) -> public-links, else the lessons context. Using the
            // lessons path for public links 404s.
            let resolvedMedia = null;
            let ppPosters = [];
            try {
              const linkContextPath = secureAccessId
                ? `/api/ui/echoplayer/secure-link-access-datas/${encodeURIComponent(secureAccessId)}/media/${encodeURIComponent(targetMediaId)}/player-properties`
                : `/api/ui/echoplayer/public-links/${encodeURIComponent(uuid)}/media/${encodeURIComponent(targetMediaId)}/player-properties`;
              const ppPaths = [
                linkContextPath,
                `/api/ui/echoplayer/lessons/${encodeURIComponent(lessonId)}/medias/${encodeURIComponent(targetMediaId)}/player-properties`,
              ];
              for (const playerPropertiesPath of ppPaths) {
                // Only stop when BOTH stream and captions are secured — an
                // early break on stream alone skips caption extraction from
                // the remaining context families.
                if (resolvedMedia && subText) break;
                const res = await echoFetch(playerPropertiesPath, { timeout: 12000 });
                ppStatus = res.status;
                console.log(`[UniSync] Player-properties ${title}: HTTP ${res.status}`);
                if (res.ok && !res.unauthenticated) {
                  const json = await res.json();
                  const pdata = json?.data || json;
                  if (pdata) {
                    if (pdata.posterUrl) ppPosters.push(pdata.posterUrl);
                    if (pdata.thumbnailUrl) ppPosters.push(pdata.thumbnailUrl);
                    const playable = pdata?.playableAudioVideo?.playableMedias || [];
                    for (const pm of playable) {
                      if (pm.posterUri) ppPosters.push(pm.posterUri);
                      if (pm.thumbnailUri) ppPosters.push(pm.thumbnailUri);
                    }
                    const jsonStr = JSON.stringify(pdata);
                    const imgHits = jsonStr.match(/https?:\/\/[^\s"'\\]+\.(?:jpg|jpeg|png)(?:\?[^\s"'\\]*)?/gi) || [];
                    for (const h of imgHits) {
                      const cleanH = h.replace(/\\u0026/g, "&").replace(/\\\//g, "/");
                      if (ppPosters.indexOf(cleanH) === -1) ppPosters.push(cleanH);
                    }
                  }
                  const captionsUrl = pdata?.captions;
                  if (!subText && captionsUrl) {
                    try {
                      const capRes = await echoFetch(captionsUrl, { timeout: 15000 });
                      if (capRes.ok) {
                        const capText = await capRes.text();
                        if (capText.trim().startsWith("WEBVTT")) subText = capText;
                      }
                    } catch (_ce) {}
                  }
                  const playable = pdata?.playableAudioVideo?.playableMedias || [];
                  const preferred = playable.find(x => (x.trackType || []).includes("Audio") && (x.trackType || []).includes("Video")) || playable.find(x => x.isHls) || playable[0];
                  if (preferred?.uri) resolvedMedia = { media_url: preferred.uri, media_kind: preferred.isHls ? "m3u8" : "mp4" };
                  const jsonStr = JSON.stringify(json);
                  const m3u8 = jsonStr.match(/https?:\/\/[^"\\]+\.m3u8[^"\\]*/i);
                  if (!resolvedMedia && m3u8) resolvedMedia = { media_url: m3u8[0], media_kind: "m3u8" };
                  else {
                    const mp4 = jsonStr.match(/https?:\/\/[^"\\]+\.mp4[^"\\]*/i);
                    if (!resolvedMedia && mp4) resolvedMedia = { media_url: mp4[0], media_kind: "mp4" };
                  }
                }
              }
            } catch (_e) {}

            // Fallback: stream URL found directly in the public page HTML.
            if (!resolvedMedia && pageStream) resolvedMedia = pageStream;

            console.log(`UniSync Embed [${title}]: mediaId=${targetMediaId}, sessId=${lessonId}, sub=${Boolean(subText || subJson)}, stream=${Boolean(resolvedMedia)}`);
            sendHeartbeat("authenticated", "Embed resolve: " + title.slice(0, 40) + " — sub=" + Boolean(subText || subJson) + " stream=" + Boolean(resolvedMedia), "echo360");

            debugResult("blaw-embed-resolve", { uuid: String(uuid), mediaId: String(targetMediaId), lessonId: String(lessonId), secure: Boolean(secureAccessId), linkId: linkId || null, ppStatus, sub: Boolean(subText || subJson), stream: Boolean(resolvedMedia) });

            // Poster harvest for embeds — same host-rewrite trick as regular lectures
            let embedB64 = null;
            let posterCandCount = 0;
            try {
              const rawThumb = pagePoster || em.thumbnailUri || em.posterUri || null;
              const posterSrcs = [];
              for (const p of ppPosters) {
                const rew = String(p)
                  .replace(/^https?:\/\/(?:[a-z0-9\-]+\.)*s3\.amazonaws\.com/i, "https://thumbnails.echo360.net.au")
                  .replace(/^https?:\/\/[a-z0-9\-]*\.?echo360\.net\.au/iu, "https://thumbnails.echo360.net.au");
                if (rew && rew !== p && posterSrcs.indexOf(rew) === -1) posterSrcs.push(rew);
                if (posterSrcs.indexOf(p) === -1) posterSrcs.push(p);
              }
              if (rawThumb) {
                const rew = String(rawThumb)
                  .replace(/^https?:\/\/(?:[a-z0-9\-]+\.)*s3\.amazonaws\.com/i, "https://thumbnails.echo360.net.au")
                  .replace(/^https?:\/\/[a-z0-9\-]*\.?echo360\.net\.au/iu, "https://thumbnails.echo360.net.au");
                if (rew && rew !== rawThumb && posterSrcs.indexOf(rew) === -1) posterSrcs.push(rew);
                if (posterSrcs.indexOf(rawThumb) === -1) posterSrcs.push(rawThumb);
              }
              // Also try the public media page for og:image
              try {
                const pubRes2 = await echoFetch("/media/" + encodeURIComponent(uuid) + "/public", { timeout: 8000 });
                if (pubRes2.ok) {
                  const pubText = await pubRes2.text();
                  const imgRe = /https?:\/\/[^\s"'\\]+\.(?:jpg|jpeg|png)(?:\?[^\s"'\\]*)?/gi;
                  let m;
                  while ((m = imgRe.exec(pubText)) !== null) {
                    const clean = m[0].replace(/\\u0026/g, "&");
                    if (posterSrcs.indexOf(clean) === -1) posterSrcs.push(clean);
                  }
                }
              } catch (_pe) {}
              posterCandCount = posterSrcs.length;
              for (const cand of posterSrcs) {
                if (embedB64) break;
                try {
                  const r = await echoFetch(cand, { responseType: "blob", timeout: 10000 });
                  if (r.ok && r.blob) {
                    const blob = await r.blob();
                    if (blob && blob.size > 50) {
                      const reader = new FileReader();
                      const bP = new Promise((res) => { reader.onloadend = () => res(reader.result); reader.onerror = () => res(null); });
                      reader.readAsDataURL(blob);
                      const b = await bP;
                      if (b && typeof b === "string" && b.length > 50) embedB64 = b;
                    }
                  }
                } catch (_e2) {}
              }
            } catch (_eh) {}
            if (embedB64) {
              try {
                const ingestRes = await new Promise((resolve) => {
                  GM_xmlhttpRequest({
                    method: "POST",
                    url: BACKEND_URL + "/api/v1/lectures/thumbnails/ingest",
                    headers: { "Content-Type": "application/json" },
                    data: JSON.stringify({ thumbnails: [{ lesson_id: "BLAW_EMBED_" + embedKey, media_id: targetMediaId, image_b64: embedB64 }] }),
                    timeout: 15000,
                    onload: (r) => resolve({ ok: r.status >= 200 && r.status < 300, text: r.responseText || "" }),
                    onerror: () => resolve({ ok: false, text: "" }),
                    ontimeout: () => resolve({ ok: false, text: "timeout" }),
                  });
                });
                if (ingestRes.ok && /"saved_count"\s*:\s*[1-9]/.test(ingestRes.text)) {
                  sendHeartbeat("authenticated", "Embed thumbnail: " + title.slice(0, 40), "echo360");
                }
              } catch (_ig2) {}
            }
            if (!embedB64 && posterCandCount > 0) {
              console.warn(`[UniSync] Poster failed for ${uuid} (${posterCandCount} candidate(s))`);
            }

            // Always list the embed, even with no subtitles/stream: the
            // classroom link alone makes it visible in the catalog, and the
            // backend only queues downloads when media/subs exist.
            try {
              const _ta = document.createElement("textarea");
              _ta.innerHTML = title;
              title = _ta.value;
            } catch (_de) {}
            embedLessons.push({
              secId: em.section_id || "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
              secName: em.course_name || "Principles of Business Law (BLAW10001_2026_SM2)",
              secKb: em.kb_name || "BLAW10001_2026_SM2",
              // Nested shape mirrors real Echo360 syllabus items
              // (catalog reads item.lesson.medias for has_media).
              lesson: {
                lesson: {
                  id: `BLAW_EMBED_${embedKey}`,
                  name: title,
                  hasMedia: Boolean(resolvedMedia),
                  timing: { start: new Date().toISOString() }
                },
                medias: targetMediaId ? [{ id: targetMediaId }] : [],
              },
              subtitles: subText,
              subtitles_json: subJson,
              media_url: resolvedMedia ? resolvedMedia.media_url : null,
              media_kind: resolvedMedia ? resolvedMedia.media_kind : "mp4",
            });
          }));
        }

        if (embedLessons.length > 0) {
          // Group embeds under their own course section (BLAW + others).
          const bySec = {};
          for (const el of embedLessons) {
            (bySec[el.secId] = bySec[el.secId] || []).push(el);
          }
          for (const secId of Object.keys(bySec)) {
            const group = bySec[secId];
            const items = group.map((el) => ({
              lesson: el.lesson,
              medias: el.medias,
              subtitles: el.subtitles,
              subtitles_json: el.subtitles_json,
              media_url: el.media_url,
              media_kind: el.media_kind,
            }));
            const sec = sections.find((s) => s.sectionId === secId);
            if (sec) {
              sec.syllabus = (sec.syllabus || []).concat(items);
            } else {
              sections.push({
                sectionId: secId,
                courseName: group[0].secName,
                name: group[0].secKb,
                syllabus: items,
              });
            }
          }
        }
      }

      // Send discovered sections directly to backend
      if (sections.length > 0) {
        GM_xmlhttpRequest({
          method: "POST",
          url: BACKEND_URL + "/api/v1/sync/payload",
          headers: { "Content-Type": "application/json" },
          data: JSON.stringify({
            url: window.location.href,
            hostname: "echo360.net.au",
            data: { type: "echo360_sections", sections: sections }
          }),
          onload: (r) => {
            console.log("UniSync: Pushed " + sections.length + " Echo360 section(s) to backend — HTTP " + r.status);
          },
          onerror: () => {
            console.warn("[UniSync] Section push failed — backend " + BACKEND_URL + " unreachable?");
          }
        });
      }
    } catch (echoErr) {
      console.debug("UniSync Echo360 sync error:", echoErr);
    } finally {
      echoSyncInFlight = false;
    }
  }

  /**
   * Autonomous Canvas Crawling & Material Harvesting.
   * Runs independently on canvas.lms.unimelb.edu.au.
   */
  async function syncCanvas() {
    if (canvasSyncInFlight || Date.now() - lastCanvasRun < 15000) return;
    canvasSyncInFlight = true;
    lastCanvasRun = Date.now();

    try {
      const res = await canvasFetch("/api/v1/courses?enrollment_type=student&enrollment_state=active&per_page=50");
      if (res.status === 401 || res.status === 403) {
        sendHeartbeat("waiting_for_auth", "Canvas LMS session expired. Please log in to Canvas.", "canvas");
        return;
      }
      if (!res.ok) return;

      sendHeartbeat("authenticated", "Canvas LMS session connected", "canvas");
      const courses = await res.json();
      if (!Array.isArray(courses) || !courses.length) return;

      let syncedFileIds = new Set();
      // Courses disabled via dashboard toggles cost zero crawl time.
      let disabledCanvasIds = new Set();
      try {
        const libData = await new Promise((resolve) => {
          GM_xmlhttpRequest({
            method: "GET",
            url: BACKEND_URL + "/api/v1/lectures/libraries",
            timeout: 10000,
            onload: (r) => {
              try { resolve(JSON.parse(r.responseText)); } catch (_e) { resolve({ libraries: [] }); }
            },
            onerror: () => resolve({ libraries: [] }),
            ontimeout: () => resolve({ libraries: [] }),
          });
        });
        for (const lib of (libData.libraries || [])) {
          if (lib.auto_sync === false) {
            disabledCanvasIds.add(String(lib.canvas_course_id || lib.id || ""));
          }
        }
      } catch (_e) {}

      // DOM scrape: embedded players (e.g. Echo360 LTI iframes) that only
      // exist in rendered page HTML, never in Canvas API bodies.
      const domEmbedsByCourse = {};
      try {
        const mCid = window.location.pathname.match(/\/courses\/(\d+)/);
        if (mCid && /canvas\.lms\.unimelb\.edu\.au/.test(window.location.hostname)) {
          const found = new Set();
          document.querySelectorAll('iframe[src*="echo360.net.au"]').forEach(f => { if (f.src) found.add(f.src); });
          document.querySelectorAll('a[href*="echo360.net.au"]').forEach(a => { if (a.href) found.add(a.href); });
          document.querySelectorAll('[data-url*="echo360.net.au"], [data-src*="echo360.net.au"]').forEach(el => {
            const v = el.getAttribute("data-url") || el.getAttribute("data-src");
            if (v) found.add(v);
          });
          if (found.size > 0) {
            domEmbedsByCourse[mCid[1]] = Array.from(found);
            console.log(`[UniSync] Canvas DOM embeds: course ${mCid[1]} — ${found.size} echo link(s)`);
          }
        }
      } catch (_de) {}
      try {
        const syncedRes = await new Promise((resolve) => {
          GM_xmlhttpRequest({
            method: "GET",
            url: BACKEND_URL + "/api/v1/sync/canvas/synced-files",
            onload: (r) => {
              try { resolve(JSON.parse(r.responseText)); } catch (_e) { resolve({ synced_file_ids: [] }); }
            },
            onerror: () => resolve({ synced_file_ids: [] })
          });
        });
        syncedFileIds = new Set((syncedRes.synced_file_ids || []).map(String));
      } catch (_e) {}

      const extractFileIds = (html) => {
        if (!html) return [];
        const ids = new Set();
        const rxList = [
          /\/files\/(\d+)/g,
          /courses\/\d+\/files\/(\d+)/g,
          /data-api-endpoint="[^"]*\/files\/(\d+)"/g,
        ];
        for (const rx of rxList) {
          let m;
          while ((m = rx.exec(html)) !== null) {
            if (m[1]) ids.add(m[1]);
          }
        }
        return Array.from(ids);
      };

      // Canvas's page API occasionally returns an empty body even though the
      // rendered page contains an external-tool iframe. Decode a couple of
      // times so both direct Echo360 links and Canvas-wrapped LTI URLs match.
      const collectEchoLinks = (raw) => {
        const matches = new Set();
        const variants = [String(raw || "")];
        for (let index = 0; index < 2; index += 1) {
          const next = [];
          for (const value of variants) {
            const normalized = value.replace(/&amp;/g, "&");
            if (!variants.includes(normalized) && !next.includes(normalized)) next.push(normalized);
            try {
              const decoded = decodeURIComponent(normalized);
              if (!variants.includes(decoded) && !next.includes(decoded)) next.push(decoded);
            } catch (_decodeErr) {}
          }
          variants.push(...next);
        }
        for (const value of variants) {
          (value.match(/(?:https?:)?\/\/echo360\.net\.au\/(?:media|lti|ui\/player)\/[a-zA-Z0-9\-]+(?:\?[^\s"'<>]*)?/gi) || [])
            .forEach((url) => matches.add(url));
        }
        return Array.from(matches);
      };

      // Some Canvas pages create the Echo iframe only after their JavaScript
      // runs.  Probe them in an off-screen, same-origin frame so discovery is
      // automatic and never navigates the user's visible Canvas page.
      const probeRenderedPage = (courseId, page) => new Promise((resolve) => {
        if (!page || !page.url) return resolve([]);
        console.log(`[UniSync] Rendered probe start: ${courseId} — ${(page.title || page.url).slice(0, 60)}`);
        const frame = document.createElement("iframe");
        let finished = false;
        const finish = (links) => {
          if (finished) return;
          finished = true;
          try { frame.remove(); } catch (_remove) {}
          console.log(`[UniSync] Rendered probe result: ${courseId} — ${(links || []).length} Echo360 link(s)`);
          resolve(links || []);
        };
        frame.setAttribute("aria-hidden", "true");
        frame.style.cssText = "position:fixed;left:-10000px;top:-10000px;width:1px;height:1px;opacity:0;pointer-events:none;border:0";
        frame.onload = () => setTimeout(() => {
          try {
            const doc = frame.contentDocument;
            const raw = doc ? doc.documentElement.outerHTML : "";
            finish(collectEchoLinks(raw));
          } catch (_probe) { finish([]); }
        }, 1800);
        frame.src = `/courses/${courseId}/pages/${encodeURIComponent(page.url)}`;
        document.documentElement.appendChild(frame);
        setTimeout(() => finish([]), 8000);
      });

      const downloadAndUploadCanvasFile = async (fileObj, courseId, courseName, folderName) => {
        const fid = String(fileObj.id || "");
        const name = fileObj.display_name || fileObj.filename || `canvas_file_${fid}.pdf`;
        const folder = folderName || fileObj.folder || "canvas";
        const downloadUrl = fileObj.url || `https://canvas.lms.unimelb.edu.au/courses/${courseId}/files/${fid}/download?download_frd=1`;

        return new Promise((resolve) => {
          GM_xmlhttpRequest({
            method: "GET",
            url: downloadUrl,
            responseType: "blob",
            timeout: 60000,
            onload: async (resp) => {
              if (resp.status >= 200 && resp.status < 300 && resp.response) {
                try {
                  const blob = resp.response;
                  const formData = new FormData();
                  formData.append("file", blob, name);
                  formData.append("course_id", String(courseId));
                  formData.append("course_name", String(courseName || ""));
                  formData.append("canvas_file_id", fid);
                  formData.append("folder", folder);
                  formData.append("filename_hint", name);

                  GM_xmlhttpRequest({
                    method: "POST",
                    url: BACKEND_URL + "/api/v1/sync/canvas/upload",
                    data: formData,
                    onload: (upResp) => {
                      if (upResp.status >= 200 && upResp.status < 300) {
                        syncedFileIds.add(fid);
                        resolve(true);
                      } else {
                        resolve(false);
                      }
                    },
                    onerror: () => resolve(false)
                  });
                } catch (e) {
                  resolve(false);
                }
              } else {
                resolve(false);
              }
            },
            onerror: () => resolve(false),
            ontimeout: () => resolve(false),
          });
        });
      };

      for (const course of courses) {
        const courseId = course.id;
        const courseName = course.name || `Course ${courseId}`;
        const candidateFiles = new Map();
        console.log(`[UniSync] Canvas crawl start: ${courseName} (${courseId})`);
        if (disabledCanvasIds.has(String(courseId))) {
          console.log(`[UniSync] Canvas crawl skip (toggled off): ${courseName} (${courseId})`);
          continue;
        }

        // 0. Discover course-specific Lecture Capture URL from Canvas navigation tabs
        try {
          const tabsRes = await canvasFetch("/api/v1/courses/" + courseId + "/tabs");
          if (tabsRes.ok) {
            const tabsList = await tabsRes.json();
            const lecTab = (Array.isArray(tabsList) ? tabsList : []).find(t => 
              /(lecture\s*capture|echo360)/i.test((t.label || "") + " " + (t.id || ""))
            );
            if (lecTab && lecTab.html_url) {
              const fullUrl = lecTab.html_url.startsWith("http") 
                ? lecTab.html_url 
                : "https://canvas.lms.unimelb.edu.au" + lecTab.html_url;
              course.lecture_capture_url = fullUrl;
              GM_setValue("last_known_lti_url", fullUrl);
            }
          }
        } catch (_te) {}

        // 1. Assignments
        try {
          const assignRes = await canvasFetch("/api/v1/courses/" + courseId + "/assignments?include[]=submission&include[]=rubric&include[]=all_dates&per_page=50");
          if (assignRes.ok) {
            course.assignments = await assignRes.json();
            for (const a of (Array.isArray(course.assignments) ? course.assignments : [])) {
              if (a.description) {
                extractFileIds(a.description).forEach(id => {
                  if (!candidateFiles.has(id)) candidateFiles.set(id, { id, folder: `assignments/${a.name || 'assignment'}` });
                });
              }
            }
          }
        } catch (_e) {}

        // 2. Modules
        try {
          const modRes = await canvasFetch("/api/v1/courses/" + courseId + "/modules?include[]=items&per_page=50");
          if (modRes.ok) {
            course.modules = await modRes.json();
            for (const mod of (Array.isArray(course.modules) ? course.modules : [])) {
              const modName = mod.name || "Module";
              for (const item of (mod.items || [])) {
                if (item.type === "File" && (item.content_id || item.id)) {
                  const fid = String(item.content_id || item.id);
                  if (!candidateFiles.has(fid)) {
                    candidateFiles.set(fid, { id: fid, display_name: item.title, folder: `modules/${modName}` });
                  }
                }
              }
            }
          }
        } catch (_e) {}

        // 3. Canvas Pages & Embedded Echo360 Lecture Links (e.g. BLAW Course Pages)
        try {
          const pagesRes = await canvasFetch("/api/v1/courses/" + courseId + "/pages?per_page=100");
          if (pagesRes.ok) {
            const pagesList = await pagesRes.json();
            if (Array.isArray(pagesList)) {
              course.pages = pagesList;
              // Prioritize lecture/video/workshop/seminar/week pages for embedded media links
              const targetPages = pagesList.filter(p => 
                /(video|lecture|workshop|seminar|test|week|case|source|pbl|\d+\.\d+)/i.test((p.title || "") + " " + (p.url || ""))
              );
              const pagesToCrawl = targetPages.length > 0 ? targetPages : pagesList.slice(0, 30);

              for (let i = 0; i < pagesToCrawl.length; i += 4) {
                const batch = pagesToCrawl.slice(i, i + 4);
                await Promise.allSettled(batch.map(async (p) => {
                  if (!p.url) return;
                  try {
                    const pDetailRes = await canvasFetch(`/api/v1/courses/${courseId}/pages/${encodeURIComponent(p.url)}`);
                    if (pDetailRes.ok) {
                      const pDetail = await pDetailRes.json();
                      const body = pDetail.body || "";
                      extractFileIds(body).forEach(id => {
                        if (!candidateFiles.has(id)) candidateFiles.set(id, { id, folder: `pages/${p.title || 'page'}` });
                      });
                      let echoMatches = collectEchoLinks(body);

                      // The page API can suppress rich iframe markup. For
                      // likely lecture pages, fetch the normal Canvas page
                      // HTML as a targeted fallback and scan its encoded LTI
                      // launch URL. This captures pages such as “1.1 How PBL
                      // Works” without an expensive all-pages browser crawl.
                      const isLikelyMediaPage = /(video|lecture|workshop|seminar|pbl)/i.test((p.title || "") + " " + (p.url || ""));
                      if (echoMatches.length === 0 && isLikelyMediaPage) {
                        try {
                          const pageHtmlRes = await canvasFetch(`/courses/${courseId}/pages/${encodeURIComponent(p.url)}`, {
                            headers: { "Accept": "text/html,application/xhtml+xml" },
                            timeout: 20000,
                          });
                          if (pageHtmlRes.ok) {
                            echoMatches = collectEchoLinks(await pageHtmlRes.text());
                            if (echoMatches.length > 0) {
                              console.log(`[UniSync] Canvas page HTML embeds: course ${courseId} page "${(p.title || p.url || "").slice(0, 50)}" — ${echoMatches.length} echo link(s)`);
                            }
                          }
                        } catch (_htmlErr) {}
                      }
                      if (echoMatches.length > 0) {
                        console.log(`[UniSync] Canvas embeds: course ${courseId} page "${(p.title || p.url || "").slice(0, 50)}" — ${echoMatches.length} echo link(s)`);
                        course.echo_embeds = course.echo_embeds || [];
                        echoMatches.forEach(em => {
                          if (!course.echo_embeds.includes(em)) course.echo_embeds.push(em);
                        });
                      }
                    }
                  } catch (_pe) {}
                }));
              }

              // BLAW uses dynamically mounted Echo players on several pages.
              // Render its media candidates invisibly after the API/HTML pass.
              if (String(courseId) === "240718") {
                for (const p of pagesToCrawl.slice(0, 80)) {
                  const cacheKey = `unisync_rendered_canvas:${courseId}:${p.url}`;
                  if (GM_getValue(cacheKey, 0) > Date.now() - 86400000) { console.log(`[UniSync] Rendered probe cached: ${(p.title || p.url).slice(0, 60)}`); continue; }
                  const renderedLinks = await probeRenderedPage(courseId, p);
                  GM_setValue(cacheKey, Date.now());
                  if (renderedLinks.length > 0) {
                    console.log(`[UniSync] Rendered Canvas embeds: course ${courseId} page "${(p.title || p.url).slice(0, 50)}" — ${renderedLinks.length} echo link(s)`);
                    course.echo_embeds = course.echo_embeds || [];
                    renderedLinks.forEach(link => { if (!course.echo_embeds.includes(link)) course.echo_embeds.push(link); });
                  }
                }
              }
            }
          }
        } catch (_e) {}

        // Download un-synced PDF or document attachments (max 15 per cycle to prevent lag)
        const unSynced = Array.from(candidateFiles.values()).filter(f => !syncedFileIds.has(String(f.id)));
        if (unSynced.length > 0) {
          const filesToDownload = unSynced.slice(0, 15);
          for (let i = 0; i < filesToDownload.length; i += 3) {
            const batch = filesToDownload.slice(i, i + 3);
            await Promise.allSettled(batch.map(async (fileObj) => {
              try {
                if (!fileObj.display_name || !fileObj.url) {
                  try {
                    const infoRes = await canvasFetch(`/api/v1/files/${fileObj.id}`);
                    if (infoRes.ok) {
                      const info = await infoRes.json();
                      fileObj.display_name = info.display_name || info.filename || fileObj.display_name;
                      fileObj.url = info.url || fileObj.url;
                    }
                  } catch (_e) {}
                }
                await downloadAndUploadCanvasFile(fileObj, courseId, courseName, fileObj.folder);
              } catch (_e) {}
            }));
          }
        }
      }

      // Merge DOM-scraped embeds into their course before submit.
      try {
        for (const [cid, urls] of Object.entries(domEmbedsByCourse)) {
          let c = courses.find(x => String(x.id) === String(cid));
          if (!c) { c = { id: Number(cid) || cid, name: `Course ${cid}` }; courses.push(c); }
          c.echo_embeds = c.echo_embeds || [];
          for (const u of urls) if (!c.echo_embeds.includes(u)) c.echo_embeds.push(u);
        }
      } catch (_me) {}

      // Update snapshot on backend
      GM_xmlhttpRequest({
        method: "POST",
        url: BACKEND_URL + "/api/v1/sync/payload",
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({ url: window.location.href, hostname: "canvas.lms.unimelb.edu.au", data: { type: "canvas_courses", courses } }),
      });
    } catch (canvasErr) {
      console.debug("UniSync Canvas sync error:", canvasErr);
    } finally {
      canvasSyncInFlight = false;
    }
  }

  /* ========================================================================== */
  /*  SECTION: OUTLOOK WEB / M365 PASSWORDLESS EMAIL CONNECTOR                  */
  /* ========================================================================== */
  let outlookSyncInFlight = false;
  let lastOutlookSync = 0;

  /**
   * Scrapes email items directly from Outlook Web DOM list items as a robust fallback
   * when Graph or OWA internal REST APIs are blocked or unavailable.
   */
  function scrapeOutlookDom() {
    const messages = [];
    try {
      const selectors = [
        '[role="option"]',
        '[role="listitem"]',
        '[role="treeitem"]',
        '[role="row"]',
        '[data-convid]',
        '[data-legacy-message-id]',
        '[aria-label*="Message"]',
        '[aria-label*="Conversation"]',
        '[aria-label*="Mail"]',
        'div.customScrollBar div[data-is-focusable="true"]',
        'div[id*="Message"]',
        'div[class*="listItem"]',
        'div[class*="Conversation"]',
      ];
      const nodes = document.querySelectorAll(selectors.join(", "));
      let idx = 0;
      const seenTexts = new Set();

      nodes.forEach((node) => {
        const text = (node.innerText || "").trim();
        if (!text || text.length < 15) return;

        // Skip sidebar / navigation headers
        if (/^(inbox|sent|drafts|deleted|archive|junk|folders|groups|favorites)/i.test(text) && text.length < 30) return;

        const fingerprint = text.slice(0, 60);
        if (seenTexts.has(fingerprint)) return;
        seenTexts.add(fingerprint);

        const lines = text.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);
        if (lines.length < 2) return;

        const subject = lines.find((l) => l.length > 3 && !l.includes("@") && !/^\d{1,2}:\d{2}/.test(l)) || lines[1] || "(No Subject)";
        const sender = lines.find((l) => l.includes("@") || (l.length < 40 && !/^\d{1,2}:\d{2}/.test(l))) || lines[0] || "University Sender";
        const preview = lines.slice(2).join(" ") || text;

        const id = node.getAttribute("data-convid") || node.getAttribute("data-legacy-message-id") || node.getAttribute("id") || `dom-${Date.now()}-${idx++}`;
        messages.push({
          uid: id,
          message_id: id,
          subject: subject,
          from: sender,
          date: new Date().toISOString(),
          text_body: preview,
        });
      });
    } catch (err) {
      console.debug("[UniSync] Outlook DOM scrape error:", err);
    }
    return messages;
  }

  /**
   * Scans browser sessionStorage and localStorage for Single Sign-On (SSO) JWT bearer tokens
   * issued during university authentication to call Microsoft Graph API directly without user credentials.
   */
  function findGraphTokensInStorage() {
    const tokens = new Set();
    try {
      const jwtRegex = /(?:eyJ0[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})/g;
      for (const storage of [sessionStorage, localStorage]) {
        for (let i = 0; i < storage.length; i++) {
          const key = storage.key(i) || "";
          const val = storage.getItem(key) || "";
          let match;
          while ((match = jwtRegex.exec(val)) !== null) {
            tokens.add(match[0]);
          }
        }
      }
    } catch (_e) {}
    return Array.from(tokens);
  }

  /**
   * Extracts the OWA session canary token from global runtime variables or session cookies.
   */
  function getOwaCanary() {
    try {
      if (window.owaUserConfig?.SessionSettings?.Canary) return window.owaUserConfig.SessionSettings.Canary;
      if (window.owaUserConfig?.Canary) return window.owaUserConfig.Canary;
      if (window.owa?.SessionSettings?.Canary) return window.owa.SessionSettings.Canary;
    } catch (_e) {}
    try {
      const match = document.cookie.match(/(?:X-OWA-CANARY|X-REQ-CNRY|X-Canary)=([^;]+)/i);
      if (match) return decodeURIComponent(match[1]);
    } catch (_e) {}
    try {
      const meta = document.querySelector('meta[name="X-OWA-CANARY"], meta[name="owa-canary"]');
      if (meta) return meta.getAttribute("content") || "";
    } catch (_e) {}
    return "";
  }

  /**
   * Queries Outlook Web App's internal FindItem JSON service using the active authenticated browser session.
   */
  async function fetchOwaFindItem() {
    const canary = getOwaCanary();
    const endpoints = [
      "/owa/service.svc?action=FindItem",
      "/owa/0/service.svc?action=FindItem",
      "/mail/service.svc?action=FindItem",
      "https://outlook.cloud.microsoft/owa/service.svc?action=FindItem",
    ];

    const findItemReq = {
      "__type": "FindItemJsonRequest:#Exchange",
      "Header": {
        "__type": "JsonRequestHeaders:#Exchange",
        "RequestVersion": "Exchange2016"
      },
      "Body": {
        "__type": "FindItemRequest:#Exchange",
        "ItemShape": {
          "BaseShape": "IdOnly",
          "AdditionalProperties": [
            { "__type": "PropertyUri:#Exchange", "PropertyId": "ItemSubject" },
            { "__type": "PropertyUri:#Exchange", "PropertyId": "MessageFrom" },
            { "__type": "PropertyUri:#Exchange", "PropertyId": "ItemDateTimeReceived" },
            { "__type": "PropertyUri:#Exchange", "PropertyId": "ItemPreview" }
          ]
        },
        "ParentFolderIds": [{ "__type": "DistinguishedFolderId:#Exchange", "Id": "inbox" }],
        "Traversal": "Shallow",
        "Paging": {
          "__type": "IndexedPageView:#Exchange",
          "BasePoint": "Beginning",
          "Offset": 0,
          "MaxEntriesReturned": 100
        }
      }
    };

    const reqHeaders = {
      "Content-Type": "application/json; charset=utf-8",
      "Action": "FindItem",
      "X-OWA-UrlPostData": "1",
    };
    if (canary) {
      reqHeaders["X-OWA-CANARY"] = canary;
      reqHeaders["X-REQ-CNRY"] = canary;
    }

    for (const endpoint of endpoints) {
      try {
        const resp = await fetch(endpoint, {
          method: "POST",
          headers: reqHeaders,
          credentials: "include",
          body: JSON.stringify(findItemReq),
        });
        const cType = resp.headers.get("content-type") || "";
        if (resp.ok && cType.includes("json")) {
          const data = await resp.json();
          const rawItems = data?.Body?.ResponseMessages?.ResponseBody?.ParentFolder?.Items ||
                           data?.ResponseMessages?.ResponseBody?.ParentFolder?.Items ||
                           data?.value || data?.Value || [];
          if (Array.isArray(rawItems) && rawItems.length > 0) {
            return rawItems.map((it) => ({
              uid: it.ItemId?.Id || it.Id || it.id,
              message_id: it.ItemId?.Id || it.Id || it.id,
              subject: it.Subject || it.subject || "(No Subject)",
              from: it.From?.Mailbox?.EmailAddress || it.from?.emailAddress?.address || it.From?.Mailbox?.Name || "University Sender",
              date: it.DateTimeReceived || it.receivedDateTime || new Date().toISOString(),
              text_body: it.Preview || it.bodyPreview || "",
            }));
          }
        }
      } catch (_e) {}
    }
    return [];
  }

  /**
   * Discovers the authenticated student email identity from browser session settings, storage, or account controls.
   */
  function detectUserAccount() {
    try {
      const settings = window.owaUserConfig?.SessionSettings || window.owa?.SessionSettings;
      if (settings?.UserEmailAddress) return settings.UserEmailAddress;
      if (settings?.UserPrincipalName) return settings.UserPrincipalName;
      if (settings?.ExplicitLogonUser) return settings.ExplicitLogonUser;
    } catch (_e) {}

    try {
      for (const storage of [sessionStorage, localStorage]) {
        for (let i = 0; i < storage.length; i++) {
          const key = storage.key(i) || "";
          const val = storage.getItem(key) || "";
          const matchUni = (key + " " + val).match(/([a-zA-Z0-9._]+@(?:student\.)?unimelb\.edu\.au)/i);
          if (matchUni) return matchUni[1];
        }
      }
    } catch (_e) {}

    try {
      for (const storage of [sessionStorage, localStorage]) {
        for (let i = 0; i < storage.length; i++) {
          const val = storage.getItem(storage.key(i) || "") || "";
          const match = val.match(/([a-zA-Z0-9._]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/);
          if (match && !match[1].includes("example") && !match[1].includes("schema")) {
            return match[1];
          }
        }
      }
    } catch (_e) {}

    try {
      const meBtn = document.querySelector('#meControl, [aria-label*="Account manager"], [data-log-name="MeControl"], [id*="O365_MainLink_Me"]');
      if (meBtn) {
        const btnText = meBtn.innerText || meBtn.getAttribute("aria-label") || meBtn.getAttribute("title") || "";
        const match = btnText.match(/([a-zA-Z0-9._]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/);
        if (match) return match[1];
      }
    } catch (_e) {}

    return "";
  }

  /**
   * Main email synchronization workflow.
   * Tiers: 1. Graph API via Session JWT -> 2. OWA FindItem API -> 3. DOM Card Scraper.
   * Ingests messages directly to local UniSync server (http://127.0.0.1:8000/api/v1/email-monitor/ingest).
   */
  async function syncOutlookEmails() {
    if (outlookSyncInFlight) return;
    const now = Date.now();
    if (now - lastOutlookSync < 15000) return; // dedup interval
    outlookSyncInFlight = true;
    lastOutlookSync = now;

    try {
      const currentEmail = detectUserAccount();
      let rawMessages = [];

      // Tier 1: Try Session Bearer token against Graph API
      const graphTokens = findGraphTokensInStorage();
      if (graphTokens.length > 0) {
        for (const token of graphTokens) {
          try {
            const gResp = await fetch(
              "https://graph.microsoft.com/v1.0/me/messages?$top=100&$select=id,subject,from,receivedDateTime,bodyPreview,body",
              {
                headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
              },
            );
            if (gResp.ok) {
              const gData = await gResp.json();
              const list = Array.isArray(gData.value) ? gData.value : Array.isArray(gData.Value) ? gData.Value : [];
              if (list.length > 0) {
                rawMessages = list.map((item, idx) => ({
                  uid: item.id || `graph-${idx}`,
                  message_id: item.id || `graph-${idx}`,
                  subject: item.subject || "(No Subject)",
                  from: item.from?.emailAddress?.address || item.from?.emailAddress?.name || "University Sender",
                  date: item.receivedDateTime || new Date().toISOString(),
                  text_body: item.bodyPreview || item.body?.content || "",
                  html_body: item.body?.contentType === "html" ? item.body?.content : null,
                }));
                break;
              }
            }
          } catch (_e) {}
        }
      }

      // Tier 2: Try OWA FindItem background services
      if (!rawMessages.length) {
        rawMessages = await fetchOwaFindItem();
      }

      // Tier 3: DOM Scraper Fallback
      if (!rawMessages.length) {
        rawMessages = scrapeOutlookDom();
      }

      if (!rawMessages.length) {
        console.debug("[UniSync] Outlook sync: No messages detected in current view.");
        return;
      }

      console.log(`[UniSync] Found ${rawMessages.length} Outlook emails. Posting to UniSync backend...`);

      GM_xmlhttpRequest({
        method: "POST",
        url: BACKEND_URL + "/api/v1/email-monitor/ingest",
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify({
          messages: rawMessages,
          source_host: window.location.hostname,
          username: currentEmail || "student@unimelb.edu.au",
          mailbox: "INBOX",
        }),
        onload: (res) => {
          try {
            const parsed = JSON.parse(res.responseText);
            console.log(`[UniSync] ✅ Ingested ${parsed.ingested ?? rawMessages.length} emails (skipped ${parsed.skipped ?? 0} duplicates).`);
          } catch (_e) {
            console.log(`[UniSync] ✅ Ingestion response status: ${res.status}`);
          }
        },
        onerror: (err) => {
          console.debug("[UniSync] Ingest request error:", err);
        },
      });
    } catch (err) {
      console.debug("[UniSync] Outlook sync error:", err);
    } finally {
      outlookSyncInFlight = false;
    }
  }

  /**
   * Opportunistic harvest on Echo360 public/player pages the user actually
   * opens: scrape rendered transcript download links + poster image from the
   * live DOM (React content the static-HTML fetch never sees) and ingest
   * them via page-context fetch (already authenticated).
   */
  async function uploadMediaBlob(blob, lessonId, title) {
    const uploadUrl = BACKEND_URL + "/api/v1/lectures/media/ingest?" + new URLSearchParams({
      lesson_id: lessonId,
      title: title || "Echo360 Video",
      course_id: "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
      course_name: "Principles of Business Law (BLAW10001_2026_SM2)",
      kb_name: "BLAW10001_2026_SM2"
    }).toString();

    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: uploadUrl,
        headers: { "Content-Type": "application/octet-stream" },
        data: blob,
        timeout: 180000,
        onload: (res) => {
          if (res.status >= 200 && res.status < 300) {
            debugResult("stream-archive-ingested", { lessonId, bytes: blob.size, status: res.status });
            resolve(res);
          } else {
            reject(new Error("Ingest endpoint failed: " + res.status));
          }
        },
        onerror: (err) => reject(new Error(err.error || "Upload network error")),
        ontimeout: () => reject(new Error("Upload timed out after 180s"))
      });
    });
  }

  async function downloadAndIngestStream(streamUrl, lessonId, title) {
    debugResult("stream-archive-start", { lessonId, streamUrl: streamUrl.slice(0, 100) });

    if (!streamUrl.includes(".m3u8")) {
      const res = await echoFetch(streamUrl, { responseType: "blob", timeout: 60000 });
      if (!res.ok || !res.response) throw new Error("Direct download failed: " + res.status);
      return uploadMediaBlob(res.response, lessonId, title);
    }

    const res = await echoFetch(streamUrl, { timeout: 20000 });
    if (!res.ok) throw new Error("Master playlist fetch failed: " + res.status);
    let text = await res.text();
    let baseUrl = streamUrl.substring(0, streamUrl.lastIndexOf("/") + 1);
    let lines = text.split("\n").map(l => l.trim()).filter(Boolean);

    const subPlaylists = lines.filter(l => !l.startsWith("#") && (l.includes(".m3u8") || l.includes("q1") || l.includes("q0")));
    let subUrl = streamUrl;
    if (subPlaylists.length > 0) {
      const targetSub = subPlaylists[subPlaylists.length - 1];
      subUrl = targetSub.startsWith("http") ? targetSub : new URL(targetSub, baseUrl).href;
      const subRes = await echoFetch(subUrl, { timeout: 20000 });
      if (!subRes.ok) throw new Error("Sub-playlist fetch failed: " + subRes.status);
      text = await subRes.text();
      baseUrl = subUrl.substring(0, subUrl.lastIndexOf("/") + 1);
      lines = text.split("\n").map(l => l.trim()).filter(Boolean);
    }

    const segUrls = [];
    for (const line of lines) {
      if (!line.startsWith("#")) {
        const u = line.startsWith("http") ? line : new URL(line, baseUrl).href;
        segUrls.push(u);
      }
    }

    if (segUrls.length === 0) throw new Error("No media segments found in playlist");
    debugResult("stream-archive-segments", { lessonId, count: segUrls.length });

    const chunks = new Array(segUrls.length);
    let nextIdx = 0;
    const concurrency = 4;
    let totalBytes = 0;

    async function worker() {
      while (nextIdx < segUrls.length) {
        const idx = nextIdx++;
        const sUrl = segUrls[idx];
        let segRes = null;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            segRes = await echoFetch(sUrl, { responseType: "arraybuffer", timeout: 30000 });
            if (segRes.ok && segRes.response && segRes.response.byteLength > 0) break;
          } catch (_) {}
          await new Promise(r => setTimeout(r, 500));
        }
        if (!segRes || !segRes.ok || !segRes.response) {
          throw new Error("Failed downloading segment " + idx);
        }
        chunks[idx] = segRes.response;
        totalBytes += segRes.response.byteLength;
      }
    }

    await Promise.all(Array.from({ length: Math.min(concurrency, segUrls.length) }, worker));
    debugResult("stream-archive-downloaded", { lessonId, totalBytes, segments: chunks.length });

    const blob = new Blob(chunks, { type: "video/mp4" });
    return uploadMediaBlob(blob, lessonId, title);
  }

  let lastPageScrape = 0;
  async function scrapeEchoPage() {
    try {
      const url = window.location.href;
      const mPage = url.match(/echo360\.net\.au\/(media\/.+\/public|ui\/player)/i);
      if (!mPage) return;
      if (Date.now() - lastPageScrape < 5000) return;
      lastPageScrape = Date.now();
      const mUuid = url.match(/(?:media|player)\/([a-f0-9\-]+)/i);
      const uuid = (mUuid && mUuid[1]) || null;

      const urlParams = new URLSearchParams(window.location.search);
      const slad = urlParams.get("secureLinkAccessDataId");
      const mediaId = urlParams.get("currentMediaId") || uuid;

      let activeTarget = null;
      try {
        const rawTarget = GM_getValue("unisync_resolving_target", "null");
        if (rawTarget && rawTarget !== "null") activeTarget = JSON.parse(rawTarget);
      } catch (_) {}

      // Fast-path: Direct authenticated resolution via player-properties API (both LTI slad and public links)
      const ppUrl = (slad && mediaId)
        ? `/api/ui/echoplayer/secure-link-access-datas/${encodeURIComponent(slad)}/media/${encodeURIComponent(mediaId)}/player-properties`
        : (uuid ? `/api/ui/echoplayer/public-links/${encodeURIComponent(uuid)}/media/${encodeURIComponent(mediaId || uuid)}/player-properties` : null);

      if (ppUrl) {
        try {
          const ppRes = await echoFetch(ppUrl, { timeout: 15000 });
          debugResult("player-properties-status", { mediaId, slad, uuid, status: ppRes.status });
          if (ppRes.ok) {
            const ppJson = await ppRes.json();
            const pdata = ppJson?.data || ppJson;

            const playable = pdata?.playableAudioVideo?.playableMedias || [];
            const preferred = playable.find(x => (x.trackType || []).includes("Audio") && (x.trackType || []).includes("Video")) || playable.find(x => x.isHls) || playable[0];
            let streamUrl = preferred?.uri || null;
            if (!streamUrl) {
              const m3u8Match = JSON.stringify(ppJson).match(/https?:\/\/[^"\\]+\.m3u8[^"\\]*/i);
              if (m3u8Match) streamUrl = m3u8Match[0];
            }
            debugResult("player-properties-debug", {
              mediaId,
              playableMedias: playable.map(x => ({ uri: (x.uri || "").slice(0, 120), isHls: x.isHls, trackType: x.trackType, mimeType: x.mimeType, label: x.label })),
              playableCount: playable.length,
              streamFound: Boolean(streamUrl),
              captionsField: typeof pdata?.captions
            });

            if (streamUrl) {
              try {
                const probeRes = await echoFetch(streamUrl, { timeout: 15000 });
                if (probeRes.ok) {
                  const pTxt = await probeRes.text();
                  debugResult("m3u8-probe-sample", {
                    mediaId,
                    status: probeRes.status,
                    lines: pTxt.split("\n").map(l => l.trim()).filter(Boolean).slice(0, 30)
                  });
                }
              } catch (prErr) {
                debugResult("m3u8-probe-error", { mediaId, error: String(prErr) });
              }
            }

            let capUrl = typeof pdata?.captions === "string" ? pdata.captions : (Array.isArray(pdata?.captions) ? (pdata.captions[0]?.url || pdata.captions[0]?.uri) : null);
            let subText = null;
            if (capUrl) {
              try {
                const capRes = await echoFetch(capUrl, { timeout: 15000 });
                if (capRes.ok) {
                  const ct = await capRes.text();
                  if (ct.trim().startsWith("WEBVTT")) subText = ct;
                }
              } catch (_) {}
            }

            if (!subText) {
              try {
                const trUrl = slad
                  ? `/api/ui/echoplayer/secure-link-access-datas/${encodeURIComponent(slad)}/medias/${encodeURIComponent(mediaId)}/transcript-file?format=vtt`
                  : (uuid ? `/api/ui/echoplayer/public-links/${encodeURIComponent(uuid)}/medias/${encodeURIComponent(mediaId || uuid)}/transcript-file?format=vtt` : null);
                if (trUrl) {
                  const trRes = await echoFetch(trUrl, { timeout: 15000 });
                  if (trRes.ok) {
                    const tt = await trRes.text();
                    if (tt.trim().startsWith("WEBVTT")) subText = tt;
                  }
                }
              } catch (_) {}
            }

            const refParams = new URLSearchParams((document.referrer || "").split("?")[1] || "");
            const effectiveLinkId = activeTarget?.link_id || urlParams.get("unisync_lid") || refParams.get("unisync_lid") || null;
            const embedKey = effectiveLinkId ? (`9381284d-5253-4e5b-b173-fb47a63576cb_` + effectiveLinkId.slice(0, 8)) : mediaId;
            const lessonId = `BLAW_EMBED_` + embedKey;

            if (subText) {
              await new Promise(res => GM_xmlhttpRequest({
                method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest",
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify({ subtitles: [{ lesson_id: lessonId, vtt: subText }] }),
                timeout: 15000, onload: res, onerror: res
              }));
              debugResult("vtt-ingested-auto", { lessonId, bytes: subText.length });
            }

            const bestTitle = activeTarget?.title || (document.title || "").replace(/ - Echo360$/i, "").trim() || "Echo360 Video";

            if (streamUrl) {
              await new Promise(res => GM_xmlhttpRequest({
                method: "POST", url: BACKEND_URL + "/api/v1/sync/payload",
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify({
                  url: window.location.href,
                  hostname: "echo360.net.au",
                  data: {
                    type: "echo360_sections",
                    sections: [{
                      sectionId: "4caa82e5-51bb-4a7c-8e49-1fe7fcf8327a",
                      courseName: "Principles of Business Law (BLAW10001_2026_SM2)",
                      name: "BLAW10001_2026_SM2",
                      syllabus: [{
                        lesson: {
                          lesson: {
                            id: lessonId,
                            name: bestTitle,
                            hasMedia: true,
                            timing: { start: new Date().toISOString() }
                          },
                          medias: [{ id: mediaId }]
                        },
                        media_url: streamUrl,
                        media_kind: /\.m3u8/i.test(streamUrl) ? "m3u8" : "mp4",
                        subtitles: subText
                      }]
                    }]
                  }
                }),
                timeout: 15000, onload: res, onerror: res
              }));
              // Stream URL and subtitles ingested into backend sync state.
              // Skip in-browser heavy segment chunking to prevent memory bloat and tab hanging.
            }

            try {
              const targetKey = activeTarget?.target_key || (activeTarget?.link_id ? (activeTarget.uuid + "_" + activeTarget.link_id) : (activeTarget?.uuid || activeTarget?.media_id || uuid));
              let act = {};
              try { act = JSON.parse(GM_getValue("unisync_active_resolving_ids", "{}")); } catch (_) {}
              if (targetKey && act[targetKey]) {
                delete act[targetKey];
                GM_setValue("unisync_active_resolving_ids", JSON.stringify(act));
              }
              GM_setValue("unisync_resolving_target", "null");
            } catch (_) {}

            if (urlParams.get("unisync_auto") === "1" || activeTarget) {
              debugResult("auto-tab-closing", { mediaId, lessonId });
              setTimeout(() => { window.close(); }, 1500);
              return;
            }
          }
        } catch (ppErr) {
          debugResult("player-properties-error", { mediaId, error: String(ppErr) });
        }
      }

      // An "Expand" action opens the player in a new tab. The Canvas page is
      // its referrer, so persist this exact secure player URL before resolving
      // anything else. This covers pages whose Canvas API body is empty.
      const refCourse = (document.referrer || "").match(/canvas\.lms\.unimelb\.edu\.au\/courses\/(\d+)/i);
      if (uuid && refCourse) {
        GM_xmlhttpRequest({
          method: "POST",
          url: BACKEND_URL + "/api/v1/sync/payload",
          headers: { "Content-Type": "application/json" },
          data: JSON.stringify({
            url,
            hostname: "echo360.net.au",
            data: { type: "canvas_courses", courses: [{ id: refCourse[1], name: "Canvas Echo360 player", echo_embeds: [url] }] },
          }),
          onload: (rr) => console.log(`[UniSync] Player handoff ${uuid} — HTTP ${rr.status}`),
          onerror: () => console.warn(`[UniSync] Player handoff failed for ${uuid}`),
        });
      }

      // Transcript download anchors (TXT/VTT buttons).
      const hrefs = Array.from(document.querySelectorAll('a[href], track[src]'))
        .map(a => a.href || a.src)
        .filter(h => /\.vtt(\?|#|$)/i.test(h) || /transcript/i.test(h));
      const tBtns = document.querySelectorAll('[data-test-id="transcript-download"]');
      reportEchoDiagnostic(`Player ${uuid || "?"}: ${hrefs.length} transcript/track URL(s), ${tBtns.length} transcript button(s)`);
      debugResult("echo-player-captions", { uuid, transcript_urls: hrefs.length, transcript_buttons: tBtns.length, track_count: document.querySelectorAll('track[src]').length });

      // Kaltura signature scan — fires whenever the player page is scraped.
      const khtml = document.documentElement.outerHTML;
      const ksig = (re) => [...new Set(khtml.match(re) || [])].slice(0, 12);
      const kframes = [...document.querySelectorAll("iframe")].map((f) => f.src).filter(Boolean).slice(0, 10);
      const kEntry = ksig(/entryId\/[a-z0-9_-]{8,40}/gi);
      const kManifest = ksig(/[^\s"'<>]*playManifest[^\s"'<>]{0,120}/gi);
      const kCaption = ksig(/[^\s"'<>]*caption_captionasset[^\s"'<>]{0,160}/gi);
      const kPartner = ksig(/\/p\/\d{2,10}/g);
      const kUiconf = ksig(/uiconf_id[\/:=][0-9.]+/gi);
      const kKs = ksig(/\/ks\/[a-zA-Z0-9._|-]{20,120}/g);
      if (kEntry.length || kManifest.length || kCaption.length || kframes.length) {
        debugResult("kaltura-signatures", { uuid, frames: kframes, entryIds: kEntry, playManifests: kManifest, captionAssets: kCaption, partnerIds: kPartner, uiconfIds: kUiconf, ksTokens: kKs });
      }
      if (kCaption.length && uuid) {
        try {
          const vRes = await echoFetch(kCaption[0], { timeout: 25000 });
          const vText = await vRes.text();
          if (vRes.ok && vText && vText.trim().startsWith("WEBVTT")) {
            await new Promise((resolve) => {
              GM_xmlhttpRequest({
                method: "POST",
                url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest",
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + uuid, vtt: vText }] }),
                timeout: 20000,
                onload: (rr) => { debugResult("kaltura-caption-ingested", { uuid, bytes: vText.length, status: rr.status }); resolve(); },
                onerror: () => resolve(),
                ontimeout: () => resolve(),
              });
            });
          } else {
            debugResult("kaltura-caption-fetch-failed", { uuid, status: vRes.status, bytes: vText.length });
          }
        } catch (_ke) {}
      }

      for (const href of hrefs.slice(0, 3)) {
        try {
          const r = await fetch(href, { credentials: "include" });
          const t = await r.text();
          debugResult("echo-player-caption-fetch", { uuid, status: r.status, bytes: t.length, webvtt: /^WEBVTT\s/i.test(t) });
          if (t && t.trim().startsWith("WEBVTT") && uuid) {
            await new Promise((resolve) => {
              GM_xmlhttpRequest({
                method: "POST",
                url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest",
                headers: { "Content-Type": "application/json" },
                data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + uuid, vtt: t }] }),
                timeout: 15000,
                onload: (rr) => { reportEchoDiagnostic("VTT ingestion for " + uuid + " — HTTP " + rr.status); resolve(); },
                onerror: () => resolve(),
                ontimeout: () => resolve(),
              });
            });
            break;
          }
        } catch (_e) {}
      }

      // Echo injects <track> after the player shell. Retry a few times so a
      // first scrape before captions mount does not permanently miss them.
      if (!hrefs.length && uuid) {
        let retries = 0;
        const retryTracks = () => {
          const tracks = Array.from(document.querySelectorAll('track[src]')).map(t => t.src).filter(Boolean);
          if (tracks.length || retries++ >= 6) {
            lastPageScrape = 0;
            return scrapeEchoPage();
          }
          setTimeout(retryTracks, 2000);
        };
        setTimeout(retryTracks, 2000);
      }

      // Poster: rendered video poster attribute or og:image.
      try {
        const v = document.querySelector("video");
        const poster = (v && v.poster) || (document.querySelector('meta[property="og:image"]') || {}).content || null;
        if (poster && uuid) {
          const pr = await fetch(poster, { credentials: "include" });
          const blob = await pr.blob();
          if (blob && blob.size > 50) {
            const reader = new FileReader();
            const b64 = await new Promise((res) => { reader.onloadend = () => res(reader.result); reader.onerror = () => res(null); reader.readAsDataURL(blob); });
            if (b64 && typeof b64 === "string" && b64.length > 50) {
              await new Promise((resolve) => {
                GM_xmlhttpRequest({
                  method: "POST",
                  url: BACKEND_URL + "/api/v1/lectures/thumbnails/ingest",
                  headers: { "Content-Type": "application/json" },
                  data: JSON.stringify({ thumbnails: [{ lesson_id: "BLAW_EMBED_" + uuid, image_b64: b64 }] }),
                  timeout: 15000,
                  onload: (rr) => { console.log("[UniSync] Page poster ingested for " + uuid + " — HTTP " + rr.status); resolve(); },
                  onerror: () => resolve(),
                  ontimeout: () => resolve(),
                });
              });
            }
          }
        }
      } catch (_pe) {}

      if (urlParams.get("unisync_auto") === "1" || activeTarget) {
        try {
          const targetKey = activeTarget?.target_key || (activeTarget?.link_id ? (activeTarget.uuid + "_" + activeTarget.link_id) : (activeTarget?.uuid || activeTarget?.media_id || uuid));
          let act = {};
          try { act = JSON.parse(GM_getValue("unisync_active_resolving_ids", "{}")); } catch (_) {}
          if (targetKey && act[targetKey]) {
            delete act[targetKey];
            GM_setValue("unisync_active_resolving_ids", JSON.stringify(act));
          }
          GM_setValue("unisync_resolving_target", "null");
        } catch (_) {}
        debugResult("auto-tab-closing", { mediaId, uuid, reason: "scrape-fallback-done" });
        setTimeout(() => { window.close(); }, 2000);
      }
    } catch (_e) {}
  }

  /**
   * Universal Sync Bridge Controller.
   * Dispatches prioritized tasks depending on current tab host.
   */
  async function runSyncBridge() {
    const isEcho = window.location.hostname.includes("echo360.net.au");
    const isCanvas = window.location.hostname.includes("canvas.lms.unimelb.edu.au");
    const isOutlook = window.location.hostname.includes("outlook") ||
                      window.location.hostname.includes("office365") ||
                      window.location.hostname.includes("office.com") ||
                      window.location.hostname.includes("cloud.microsoft");

    const isPerusall = window.location.hostname.includes("perusall.com");

    if (isOutlook) {
      sendHeartbeat("authenticated", "Active on Outlook Web: " + window.location.hostname, "outlook");
      await syncOutlookEmails();
      return;
    }

    if (isPerusall) {
      sendHeartbeat("authenticated", "Connected to Perusall: " + (document.title || "Reading Platform"), "perusall");
      return;
    }

    sendHeartbeat("authenticated", "Active on " + window.location.hostname, isEcho ? "echo360" : (isCanvas ? "canvas" : "generic"));

    if (isEcho) {
      // User is on Echo360: prioritize Echo360 sync immediately!
      await syncEcho360();
      await scrapeEchoPage();
    } else if (isCanvas) {
      watchCanvasEmbedMounts();
      // User is on Canvas: run both in parallel
      await Promise.allSettled([
        syncEcho360(),
        syncCanvas(),
      ]);
    } else {
      await Promise.allSettled([
        syncEcho360(),
        syncCanvas(),
      ]);
    }
  }

  // Continuous scroll listener for Outlook inbox view
  if (window.location.hostname.includes("outlook") || window.location.hostname.includes("office") || window.location.hostname.includes("cloud.microsoft")) {
    let scrollTimeout = null;
    bridgeLifecycle.on(window, "scroll", () => {
      if (scrollTimeout) return;
      scrollTimeout = setTimeout(() => {
        scrollTimeout = null;
        syncOutlookEmails();
      }, 3000);
    }, { passive: true });
  }

  // Passive transcript interceptor: wraps fetch + XHR so that whenever a REAL Echo player
  // fetches its own transcript JSON (with whatever auth headers it uses), we capture the
  // cue list and ingest it. Runs on every echo page incl. LTI iframes.
  (function () {
    if (window.__unisyncTranscriptHook) return; window.__unisyncTranscriptHook = true;
    const MAP = { "91adb8a8-b1ba-40d8-8bd5-8f6a3650a16a": "9381284d-5253-4e5b-b173-fb47a63576cb" };
    const fmt = (ms) => { const t = Math.max(0, Math.floor(ms)); const p = (n, l) => String(n).padStart(l, "0"); return p(Math.floor(t / 3600000), 2) + ":" + p(Math.floor((t % 3600000) / 60000), 2) + ":" + p(Math.floor((t % 60000) / 1000), 2) + "." + p(t % 1000, 3); };
    const handle = (url, txt) => { try {
      const us = String(url || ""); if (!us || !txt) return;
      const low = us.toLowerCase();
      if (us.indexOf("/transcript") === -1 && low.indexOf(".vtt") === -1 && low.indexOf("caption") === -1 && low.indexOf("captions") === -1) return;
      let vtt = "";
      if (txt.trim().toUpperCase().indexOf("WEBVTT") === 0) { vtt = txt; }
      else {
        const j = JSON.parse(txt);
        const cues = Array.isArray(j) ? j : (Array.isArray(j.transcript) ? j.transcript : (Array.isArray(j.cues) ? j.cues : []));
        if (!cues.length || (cues[0].startMs === undefined && cues[0].start === undefined)) return;
        for (const c of cues) { const st = c.startMs !== undefined ? c.startMs : c.start; const en = c.endMs !== undefined ? c.endMs : c.end; vtt += fmt(st) + " --> " + fmt(en) + "\n" + String(c.content || c.text || "").replace(/\n+/g, " ") + "\n\n"; }
      }
      const seg = us.split("/medias/"); const mid = (seg[1] || "").split("/")[0] || "unknown";
      const key = MAP[mid] || mid;
      debugResult("transcript-hook", { key, bytes: vtt.length, webvtt: vtt.slice(0, 6).toUpperCase() === "WEBVTT", url: us.slice(0, 140) });
      GM_xmlhttpRequest({ method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest", headers: { "Content-Type": "application/json" }, data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + key, vtt: vtt }] }), timeout: 20000, onload: (r) => debugResult("transcript-hook-ingested", { key, status: r.status, bytes: vtt.length }), onerror: () => {} });
    } catch (_e) {} };
    const of = window.fetch;
    if (of) { window.fetch = function (...a) { const pr = of.apply(this, a); pr.then((r) => { try { const u = (a[0] && a[0].url) || a[0]; if (u && String(u).indexOf("/transcript") !== -1) { r.clone().text().then((t) => handle(u, t)).catch(() => {}); } } catch (_e) {} }).catch(() => {}); return pr; }; }
    const oo = XMLHttpRequest.prototype.open; XMLHttpRequest.prototype.open = function (m, u) { this.__usyncUrl = u; return oo.apply(this, arguments); };
    const os = XMLHttpRequest.prototype.send; XMLHttpRequest.prototype.send = function () { this.addEventListener("load", () => { try { if (this.__usyncUrl && String(this.__usyncUrl).indexOf("/transcript") !== -1) handle(this.__usyncUrl, this.responseText); } catch (_e) {} }); return os.apply(this, arguments); };
  })();

  // Periodic heartbeat: run autonomously every 15s in background
  try {
    const _v = (typeof GM_info !== "undefined" && GM_info.script && GM_info.script.version) || "unknown";
    console.log("[UniSync] Bridge v" + _v + " loaded on " + window.location.hostname);
  } catch (_e) {}
  window.__unisync_bridge_timer = setInterval(runSyncBridge, 15000);
  // ── Bridge command pipe (FIFO, verbose titles) — target ""=any, "echo360.net.au"=Echo — polls every 3s ──
  window.__unisync_command_timer = setInterval(() => GM_xmlhttpRequest({
    method: "GET",
    url: BACKEND_URL + "/api/v1/sync/bridge/command?tab=" + encodeURIComponent(window.location.href) + "&tab_id=" + encodeURIComponent(TAB_ID),
    onload: (r) => {
      try {
        const c = JSON.parse(r.responseText);
        const command = String(c.command || "");
        if (command) debugResult("command-received", { command: command.slice(0,80), target: String(c.target||"").slice(0,60), url: window.location.href.slice(0,120), title: (document.title||"").slice(0,60) });
      if (command === "reset_resolver") { try { GM_setValue("unisync_attempt_counts", "{}"); GM_setValue("unisync_attempt_times", "{}"); GM_setValue("unisync_active_resolving_ids", "{}"); GM_setValue("unisync_resolving_target", "null"); debugResult("resolver-reset-done", { ok: true }); } catch (_) {} setTimeout(runAutoResolver, 1000); return; }
      if (command.startsWith("open_echo_player|")) { const targetUrl = command.slice("open_echo_player|".length); if (/^https:\/\/(?:echo360\\.net\\.au|canvas\\.lms\\.unimelb\\.edu\\.au)\//i.test(targetUrl)) window.location.assign(targetUrl); return; }
      if (command === "dump_dom") { const frames = [...document.querySelectorAll("iframe")].map(f => ({ src: (f.src || "").slice(0, 250), id: f.id || "", cls: (f.className || "").slice(0, 80), w: f.clientWidth, h: f.clientHeight })); const vids = [...document.querySelectorAll("video")].map(v => ({ src: (v.currentSrc || v.src || "").slice(0, 250) })); const tracks = [...document.querySelectorAll("track")].map(t => ({ src: (t.src || "").slice(0, 250), kind: t.kind || "", label: t.label || "" })); const html0 = document.documentElement.outerHTML; const kalt = [...new Set(html0.match(/kaltura[^\s"'"<>]{0,140}/gi) || [])].slice(0, 10); const uniq = (re) => [...new Set(html0.match(re) || [])].slice(0, 12); const entryIds = uniq(/entryId\/[a-z0-9_-]{8,40}/gi); const playManifests = uniq(/[^\s"'<>]*playManifest[^\s"'<>]{0,120}/gi); const captionAssets = uniq(/[^\s"'<>]*caption_captionasset[^\s"'<>]{0,160}/gi); const partnerIds = uniq(/\/p\/\d{2,10}/g); debugResult("dom-dump", { url: window.location.href.slice(0, 120), frames, videos: vids, tracks, kalturaStrings: kalt, entryIds, playManifests, captionAssets, partnerIds }); return; }
      if (command.startsWith("open_new_tab|")) { const newTabUrl = command.slice("open_new_tab|".length); if (newTabUrl.startsWith("https://echo360.net.au/") || newTabUrl.startsWith("https://canvas.lms.unimelb.edu.au/")) { let opened = false; if (typeof _gmOpenInTab === "function") { try { _gmOpenInTab(newTabUrl, { active: false, insert: true }); opened = true; } catch (_gt) {} } if (!opened) { try { opened = !!window.open(newTabUrl, "_blank"); } catch (_wo) {} } // popup-blocked fallback: navigate current tab when it is already on echo/canvas (no grant needed)
                    if (!opened && /echo360\.net\.au|canvas\.lms\.unimelb\.edu\.au/i.test(window.location.hostname)) { try { window.location.assign(newTabUrl); opened = true; } catch (_nav) {} }
                    debugResult("tab-opened", { url: newTabUrl.slice(0, 160), method: opened ? (typeof _gmOpenInTab === "function" ? "gm" : (window.location.href !== newTabUrl ? "navigate" : "window")) : "blocked", grantPresent: typeof _gmOpenInTab === "function" }); } return; }
      if (command === "play_player") { const sels = ["[id=\"player-controls-play\"]", "[id=\"play-circle\"]", "[class*=\"play-button\"]", "[class*=\"big-play\"]", "[aria-label*=\"Play\" i]", "button[class*=\"play\" i]"]; let clicked = null; for (const s of sels) { const el = document.querySelector(s); if (el) { try { el.click(); } catch (_ce) {} clicked = s; break; } } if (!clicked) { const v = document.querySelector("video"); if (v) { try { v.play(); clicked = "video.play()"; } catch (_pe) {} } } if (!clicked) { const cands = [...document.querySelectorAll("button,[role=button],[id*=play],[class*=play]")].slice(0, 25).map((b) => ({ id: b.id || "", cls: String(b.className).slice(0, 60), label: (b.getAttribute("aria-label") || b.textContent || "").trim().slice(0, 40) })); debugResult("play-probe", { clicked: null, candidates: cands }); } else { reportEchoDiagnostic("Player play clicked: " + clicked); } setTimeout(() => { lastPageScrape = 0; scrapeEchoPage(); }, 4000); return; }
      if (command === "dump_resources") { const uuid = (window.location.href.match(/(?:media|player)\/([a-f0-9-]+)/i) || [])[1] || null; const urls = performance.getEntriesByType("resource").map(e => e.name).filter(u => /m3u8|mp4|vtt|caption|subtitle|transcript|media|stream/i.test(u)).map(u => u.split("?")[0]).filter((u, i, a) => a.indexOf(u) === i).slice(-100); debugResult("echo-player-resources", { uuid, count: urls.length, urls }); return; }
      if (command === "enable_captions") { const el = document.querySelector('[id="close-caption-menu-toggle-btn"]'); if (el) { el.click(); reportEchoDiagnostic("Caption toggle clicked"); } setTimeout(() => { lastPageScrape = 0; scrapeEchoPage(); }, 2500); return; }
      if (command === "scrape_echo_page") { lastPageScrape = 0; scrapeEchoPage(); reportEchoDiagnostic("Executed backend command: scrape_echo_page"); }
      if (command.startsWith("dump_html|")) { const parts = command.split("|"); const p = parts[1]; const tag = parts[2] || "html"; echoFetch(p, { timeout: 15000 }).then(async (r) => { const text = await r.text(); const pats = { kaltura: /kaltura[^\"'\s<>]{0,160}/gi, partner: /partner[ _-]?id[\"'\\s:=]+[0-9a-z-]{2,60}/gi, uiconf: /uiconf[ _-]?id[\"'\\s:=]+[0-9a-z-]{2,60}/gi, entryId: /entry[ _-]?id[\"'\\s:=]+[0-9a-z_-]{4,60}/gi, vtt: /https?:\/\/[^\"'\s<>]+\.vtt[^\"'\s<>]*/gi, caption: /https?:\/\/[^\"'\s<>]*caption[^\"'\s<>]*/gi, mp4: /https?:\/\/[^\"'\s<>]+\.mp4[^\"'\s<>]*/gi }; const found = {}; for (const [k, re] of Object.entries(pats)) { const m = text.match(re) || []; found[k] = [...new Set(m)].slice(0, 8); } debugResult("html-dump", { tag, path: p, status: r.status, bytes: text.length, title: (text.match(/<title>([^<]*)<\/title>/i) || [])[1] || null, found }); }).catch((e) => debugResult("html-dump", { tag, path: p, error: String(e) })); return; }
      if (command.startsWith("dump_json|")) { const parts = command.split("|"); const p = parts[1]; const tag = parts[2] || "dump"; echoFetch(p, { timeout: 15000 }).then(async (r) => { let j = null; try { j = await r.json(); } catch (_e) {} if (!j) { debugResult("json-dump", { tag, path: p, status: r.status, note: "non-json" }); return; } const captionish = []; const walk = (obj, path) => { if (obj === null || obj === undefined) return; if (typeof obj === "string") { if (/capt|trans|vtt|subtitle/i.test(obj) && /^https?:/.test(obj)) captionish.push({ path, url: obj.slice(0, 300) }); return; } if (typeof obj !== "object") return; for (const k of Object.keys(obj)) walk(obj[k], path + "." + k); }; walk(j, ""); const dataObj = (j && typeof j.data === "object" && j.data) ? j.data : {}; debugResult("json-dump", { tag, path: p, status: r.status, topKeys: Object.keys(j), dataKeys: Object.keys(dataObj), captionish: captionish.slice(0, 10), dataPreview: JSON.stringify(j.data || j).slice(0, 2500) }); }).catch((e) => debugResult("json-dump", { tag, path: p, error: String(e) })); return; }
      if (command.startsWith("probe_echo|")) { const p = command.slice("probe_echo|".length); echoFetch(p, { timeout: 12000 }).then(async (r) => { let fullText = ""; try { fullText = await r.text(); } catch (_e) {} const body = fullText.slice(0, 4000); let topKeys = []; try { const j = JSON.parse(fullText || "{}"); topKeys = Object.keys(j?.data || j || {}); } catch (_e) {} debugResult("echo-probe", { path: p.split("?")[0], status: r.status, ok: r.ok, sample: body, topKeys, bytes: fullText.length }); }).catch((e) => debugResult("echo-probe", { path: p.split("?")[0], error: String(e) })); return; }
      if (command === "check_cookies") {
        const raw = document.cookie || "";
        const pairs = raw.split(";").map(s => {
          const idx = s.indexOf("=");
          return idx === -1 ? null : { domain: window.location.hostname, name: s.slice(0, idx).trim(), value: s.slice(idx + 1).trim() };
        }).filter(Boolean);
        const names = pairs.map(p => p.name);
        debugResult("cookie-check", { host: window.location.hostname, count: pairs.length, names });
        if (pairs.length) {
          GM_xmlhttpRequest({
            method: "POST", url: BACKEND_URL + "/api/v1/sync/credentials",
            headers: { "Content-Type": "application/json" },
            data: JSON.stringify({ cookies: pairs })
          });
        }
        return;
      }
      
      if (command.startsWith("fetch_lti|")) {
        const parts = command.split("|");
        const uuid = parts[1] || "";
        const linkId = parts[2] || "";
        const ltiUrl = "https://canvas.lms.unimelb.edu.au/courses/240718/external_tools/retrieve?display=borderless&url=https://echo360.net.au/lti/" + encodeURIComponent(uuid) + (linkId ? "?linkId=" + encodeURIComponent(linkId) : "") + "&autoplay=false&automute=false";
        debugResult("lti-fetch-start", { uuid, linkId, url: ltiUrl.slice(0,120) });
        canvasFetch(ltiUrl, { timeout: 15000 }).then(async (r) => {
          let html = "";
          try { html = await r.text(); } catch (_e) {}
          debugResult("lti-fetch-done", { uuid, status: r.status, bytes: html.length, hasPlayer: html.indexOf("/ui/player") !== -1, hasSlad: html.indexOf("secureLinkAccessDataId") !== -1 });
          // Extract player URL (Canvas LTI returns 302 or HTML with form/player link)
          let playerUrl = null;
          const mP = html.match(/https:\/\/echo360\.net\.au\/ui\/player\/[a-f0-9-]+\?[^"' \t\n\r<>]+/i);
          if (mP) playerUrl = mP[0].replace(/&#x2F;/g, "/").replace(/&amp;/g, "&");
          if (playerUrl) {
            try {
              const path = playerUrl.replace("https://echo360.net.au", "");
              const pr = await echoFetch(path, { timeout: 12000 });
              let pHtml = "";
              try { pHtml = await pr.text(); } catch (_e) {}
              debugResult("lti-player-html", { status: pr.status, bytes: pHtml.length });
              const sl = (pHtml.match(/secureLinkAccessDataId=([a-f0-9-]{36})/i) || playerUrl.match(/secureLinkAccessDataId=([a-f0-9-]{36})/i) || [])[1];
              const mid = (pHtml.match(/currentMediaId=([a-f0-9-]{36})/i) || playerUrl.match(/currentMediaId=([a-f0-9-]{36})/i) || [])[1] || uuid;
              if (sl && mid) {
                const tp = "/api/ui/echoplayer/secure-link-access-datas/" + encodeURIComponent(sl) + "/medias/" + encodeURIComponent(mid) + "/transcript";
                const tr = await echoFetch(tp, { timeout: 12000 });
                let tj = "";
                try { tj = await tr.text(); } catch (_e) {}
                debugResult("lti-transcript-probe", { path: tp.split("?")[0], status: tr.status, bytes: tj.length, sample: tj.slice(0,200).replace(/\s+/g, " ").slice(0,180) });
                if (tr.ok) {
                  let j = null;
                  try { j = JSON.parse(tj); } catch (_e) {}
                  if (j) {
                    let arr = Array.isArray(j) ? j : (Array.isArray(j.transcript) ? j.transcript : (Array.isArray(j.cues) ? j.cues : (Array.isArray(j.data) ? j.data : [])));
                    arr = arr.filter(c => c && (c.startMs !== undefined || c.start !== undefined) && (c.content || c.text));
                    if (arr.length) {
                      const fmt = (ms) => { const v = Math.max(0, Math.floor(ms)); const h = String(Math.floor(v / 3600000)).padStart(2,"0"); const m = String(Math.floor((v % 3600000)/60000)).padStart(2,"0"); const s = String(Math.floor((v % 60000)/1000)).padStart(2,"0"); const f = String(v % 1000).padStart(3,"0"); return h+":"+m+":"+s+"."+f; };
                      let vtt = "WEBVTT\n\n";
                      for (const c of arr) { const st = c.startMs !== undefined ? c.startMs : c.start; const en = c.endMs !== undefined ? c.endMs : c.end; vtt += fmt(st) + " --> " + fmt(en) + "\n" + String(c.content || c.text || "").replace(/\n+/g, " ") + "\n\n"; }
                      await new Promise(res => {
                        GM_xmlhttpRequest({ method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest", headers: { "Content-Type": "application/json" }, data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + uuid, vtt: vtt }] }), timeout: 20000, onload: (ir) => { debugResult("lti-transcript-ingested", { uuid, cues: arr.length, bytes: vtt.length, status: ir.status }); res(); }, onerror: () => res(), ontimeout: () => res() });
                      });
                    } else {
                      debugResult("lti-transcript-empty", { uuid, topKeys: j ? Object.keys(j) : [] });
                    }
                  }
                }
              }
            } catch (_e5) { debugResult("lti-player-error", { uuid, error: String(_e5) }); }
          } else {
            debugResult("lti-no-player", { uuid, status: r.status, snippet: html.slice(0,600).replace(/\s+/g, " ").slice(0,400) });
          }
        }).catch(e => debugResult("lti-fetch-error", { uuid, error: String(e) }));
        return;
      }
      if (command.startsWith("fetch_embed_manifest|")) { const parts = command.split("|"); const manifestUrl = parts[1]; const embedKey = parts[2] || "unknown"; echoFetch(manifestUrl, { timeout: 20000 }).then(async (r) => { const text = await r.text(); const lines = text.split("\n"); const subRenditions = []; for (let li = 0; li < lines.length; li++) { if (/TYPE=SUBTITLES/i.test(lines[li])) { const nm = (lines[li].match(/NAME="([^"]*)"/i) || [])[1] || "subtitles"; const uri = (lines[li + 1] || "").match(/^(?:#EXT-X-MEDIA[^\n]*?)?URI="([^"]+)"/i) || (lines[li + 1] || "").trim().match(/^(https?:\/\/\S+\.m3u8\S*)$/i); const rawUri = uri ? uri[1] : null; subRenditions.push({ name: nm, uri: rawUri }); } } const vttUrls = (text.match(/https?:\/\/[^\s"']+\.vtt[^\s"']*/gi) || []).slice(0, 5); debugResult("embed-manifest", { key: embedKey, status: r.status, bytes: text.length, subRenditions, vttUrls }); if (subRenditions.length === 0 && vttUrls.length === 0) return; const leg = subRenditions.find(s => /en|english/i.test(s.name)) || subRenditions[0]; const abs = (u) => { try { return new URL(u, manifestUrl).href; } catch (_e) { return null; } }; const candidates = []; if (leg && leg.uri) candidates.push(abs(leg.uri)); for (const v of vttUrls) candidates.push(v); for (const c of candidates.filter(Boolean)) { try { const vRes = await echoFetch(c, { timeout: 20000 }); if (!vRes.ok) continue; const vText = await vRes.text(); if (vText && vText.trim().startsWith("WEBVTT")) { await new Promise((res) => { GM_xmlhttpRequest({ method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest", headers: { "Content-Type": "application/json" }, data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + embedKey, vtt: vText }] }), timeout: 20000, onload: (ir) => res({ ok: ir.status >= 200 && ir.status < 300, text: ir.responseText || "" }), onerror: () => res({ ok: false, text: "" }), ontimeout: () => res({ ok: false, text: "timeout" }) }); }); debugResult("embed-subtitle", { key: embedKey, source: c.split("?")[0], bytes: vText.length }); return; } } catch (_e) {} } }).catch((e) => debugResult("embed-manifest", { key: embedKey, error: String(e) })); return; }
      if (command.startsWith("ingest_json_transcript|")) { const parts = command.split("|"); const tp = parts[1]; const ek = parts[2] || "unknown"; echoFetch(tp, { timeout: 20000 }).then(async (r) => { let j = null; try { j = await r.json(); } catch (_e) {} if (!r.ok || !j) { debugResult("json-transcript", { key: ek, status: r.status, ok: false, note: "no json" }); return; } let cues = Array.isArray(j) ? j : (Array.isArray(j.transcript) ? j.transcript : (Array.isArray(j.cues) ? j.cues : (Array.isArray(j.data) ? j.data : []))); cues = cues.filter((c) => c && (c.startMs !== undefined || c.start !== undefined) && (c.content || c.text)); if (!cues.length) { debugResult("json-transcript", { key: ek, status: r.status, ok: true, cues: 0, topKeys: Object.keys(j) }); return; } const fmt = (ms) => { const t = Math.max(0, Math.floor(ms)); const h = String(Math.floor(t / 3600000)).padStart(2, "0"); const m = String(Math.floor((t % 3600000) / 60000)).padStart(2, "0"); const s = String(Math.floor((t % 60000) / 1000)).padStart(2, "0"); const f = String(t % 1000).padStart(3, "0"); return h + ":" + m + ":" + s + "." + f; }; let vtt = "WEBVTT\n\n"; for (const c of cues) { const st = c.startMs !== undefined ? c.startMs : c.start; const en = c.endMs !== undefined ? c.endMs : c.end; vtt += fmt(st) + " --> " + fmt(en) + "\n" + String(c.content || c.text || "").replace(/\n+/g, " ") + "\n\n"; } await new Promise((resolve) => { GM_xmlhttpRequest({ method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest", headers: { "Content-Type": "application/json" }, data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + ek, vtt: vtt }] }), timeout: 20000, onload: (ir) => { debugResult("json-transcript", { key: ek, ok: true, cues: cues.length, bytes: vtt.length, ingest: ir.status }); resolve(); }, onerror: () => resolve(), ontimeout: () => resolve() }); }); }).catch((e) => debugResult("json-transcript", { key: ek, error: String(e) })); return; }
      if (command.startsWith("ingest_vtt_url|")) { const parts = command.split("|"); const vttUrl = parts[1]; const embedKey = parts[2] || "unknown"; echoFetch(vttUrl, { timeout: 20000 }).then(async (r) => { if (!r.ok) return; const vText = await r.text(); if (vText && vText.trim().startsWith("WEBVTT")) { await new Promise((res) => { GM_xmlhttpRequest({ method: "POST", url: BACKEND_URL + "/api/v1/lectures/subtitles/ingest", headers: { "Content-Type": "application/json" }, data: JSON.stringify({ subtitles: [{ lesson_id: "BLAW_EMBED_" + embedKey, vtt: vText }] }), timeout: 20000, onload: (ir) => { debugResult("embed-subtitle", { key: embedKey, status: ir.status, bytes: vText.length }); res(); }, onerror: () => res(), ontimeout: () => res() }); }); } }).catch((e) => debugResult("embed-subtitle-err", { error: String(e) })); return; }
      if (command === "resolve_embed_catalog") { lastEchoRun = 0; reportEchoDiagnostic("Embed catalog resolve requested"); syncEcho360().catch(() => {}); return; }       } catch (_e) {}
    }
  }), 3000);
  if (/echo360\.net\.au$/i.test(window.location.hostname)) setTimeout(() => { lastPageScrape = 0; scrapeEchoPage().catch(() => {}); }, 2500);
})(
  typeof GM_xmlhttpRequest !== "undefined" ? GM_xmlhttpRequest : undefined,
  typeof GM_getValue !== "undefined" ? GM_getValue : undefined,
  typeof GM_setValue !== "undefined" ? GM_setValue : undefined,
  typeof GM_openInTab !== "undefined" ? GM_openInTab : undefined,
  typeof setTimeout !== "undefined" ? setTimeout : undefined,
  typeof setInterval !== "undefined" ? setInterval : undefined,
  typeof MutationObserver !== "undefined" ? MutationObserver : undefined,
  typeof fetch !== "undefined" ? fetch : undefined,
  typeof bridgeLifecycle !== "undefined" ? bridgeLifecycle : undefined
);
