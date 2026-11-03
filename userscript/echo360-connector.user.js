// ==UserScript==
// @name         UniSync Sync Bridge (DEPRECATED)
// @deprecated   DEPRECATED: UniSync has migrated to Camoufox stealth browser. See backend/unisync/services/browser/camoufox_service.py
// @version      4.0.3
// @updateURL    http://127.0.0.1:8000/api/v1/lectures/echo360/connector.user.js
// @downloadURL  http://127.0.0.1:8000/api/v1/lectures/echo360/connector.user.js
// @description  [DEPRECATED] UniSync v3 Universal Sync Bridge — Replaced by Camoufox autonomous stealth browser.
// @match        *://echo360.net.au/*
// @match        *://*.echo360.net.au/*
// @match        *://canvas.lms.unimelb.edu.au/*
// @match        *://*.unimelb.edu.au/*
// @match        *://edstem.org/*
// @match        *://*.edstem.org/*
// @match        *://outlook.office.com/*
// @match        *://*.outlook.office.com/*
// @match        *://outlook.office365.com/*
// @match        *://*.outlook.office365.com/*
// @match        *://outlook.cloud.microsoft/*
// @match        *://*.outlook.cloud.microsoft/*
// @match        *://app.perusall.com/*
// @match        *://*.perusall.com/*
// @match        http://localhost:*/*
// @match        http://127.0.0.1:*/*
// @match        https://localhost:*/*
// @match        https://127.0.0.1:*/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_openInTab
// @connect      127.0.0.1
// @connect      localhost
// @connect      echo360.net.au
// @connect      *.echo360.net.au
// @connect      canvas.lms.unimelb.edu.au
// @connect      *.inscloudgate.net
// @connect      *.amazonaws.com
// @connect      app.perusall.com
// @connect      *.perusall.com
// @connect      outlook.office.com
// @connect      outlook.office365.com
// @connect      outlook.cloud.microsoft
// @connect      graph.microsoft.com
// @run-at       document-idle
// ==/UserScript==


// Stable bootstrap: application code lives on the local UniSync backend.
(function () {
  "use strict";
  console.warn("[UniSync] Userscript bridge is DEPRECATED and archived. UniSync now uses Camoufox autonomous stealth browser.");
  const config = { version: "4.0.3" };
  const base = "http://127.0.0.1:8000";
  let source = "", dispose = null, loading = false;
  function start(code) {
    // Compile before stopping the working runtime.
    const names = ["GM_xmlhttpRequest", "GM_getValue", "GM_setValue", "GM_openInTab",
      "setTimeout", "setInterval", "MutationObserver", "fetch", "bridgeLifecycle"];
    const run = new Function(...names, code);
    if (dispose) dispose();
    let active = true;
    const timers = new Set(), requests = new Set(), observers = new Set();
    const abort = new AbortController();
    const lifecycle = {
      version: config.version,
      active: () => active,
      on: (target, event, callback, options = {}) =>
        target.addEventListener(event, callback, {...options, signal: abort.signal}),
    };
    const timer = repeat => (callback, delay, ...args) => {
      if (!active) return 0;
      const id = (repeat ? setInterval : setTimeout)(() => {
        if (!repeat) timers.delete(id);
        if (active) callback(...args);
      }, delay);
      timers.add(id);
      return id;
    };
    const request = options => {
      if (!active) return {abort() {}};
      const wrapped = {...options};
      for (const key of ["onload", "onerror", "ontimeout", "onabort", "onprogress"]) {
        if (typeof options[key] === "function")
          wrapped[key] = (...args) => { if (active) options[key](...args); };
      }
      const handle = GM_xmlhttpRequest(wrapped);
      requests.add(handle);
      return handle;
    };
    class Observer extends MutationObserver {
      constructor(callback) {
        super((...args) => { if (active) callback(...args); });
        observers.add(this);
      }
    }
    dispose = () => {
      active = false;
      abort.abort();
      timers.forEach(id => { clearTimeout(id); clearInterval(id); });
      requests.forEach(r => { try { r.abort(); } catch (_) {} });
      observers.forEach(o => o.disconnect());
    };
    try {
      if (typeof GM_setValue === "function") {
        GM_setValue("unisync_loader_version", config.version);
      }
      run(request, GM_getValue, (...args) => { if (active) GM_setValue(...args); },
        typeof GM_openInTab === "function" ? GM_openInTab : null,
        timer(false), timer(true), Observer,
        (url, options = {}) => fetch(url, {...options, signal: abort.signal}), lifecycle);
    } catch (error) {
      dispose();
      throw error;
    }
    source = code;
    console.info("[UniSync loader] Backend runtime activated", config.version);
  }
  function refresh() {
    if (loading) return;
    loading = true;
    GM_xmlhttpRequest({
      method: "GET", url: base + "/userscript/bridge-runtime.js?t=" + Date.now(),
      headers: {"Cache-Control": "no-cache"}, timeout: 8000,
      onload: r => {
        loading = false;
        if (r.status !== 200 || !r.responseText.startsWith("// UniSync backend runtime")) {
          console.warn("[UniSync loader] Runtime unavailable", r.status); return;
        }
        if (r.responseText !== source) {
          try { start(r.responseText); }
          catch (error) { console.error("[UniSync loader] Runtime activation failed", error); }
        }
      },
      onerror: () => { loading = false; },
      ontimeout: () => { loading = false; },
    });
  }
  refresh();
  setInterval(refresh, 5000);
})();
