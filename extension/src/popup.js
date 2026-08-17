/*
 * Popup. Three things happen here that cannot happen anywhere else:
 *
 *   - chrome.permissions.request() is called. It needs a real user gesture, and
 *     a service worker never has one, so every grant flows through a click in
 *     this window. The worker decides which patterns to ask for; this file only
 *     asks, and reports back.
 *   - activeTab is exercised. Opening the popup grants it for the current tab,
 *     which is the only reason tabs.query can read a url without the far
 *     broader "tabs" permission.
 *   - "Inspect page" surfaces exactly what the adapters saw. On a site nobody
 *     has written an adapter for, that output is the whole debugging story.
 */

const REPO = "https://github.com/KernelSpecter/nowwatching";

const $ = (id) => document.getElementById(id);

let current = { host: null, url: null, tabId: null };

function ask(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (reply) => {
      void chrome.runtime.lastError;
      resolve(reply || null);
    });
  });
}

function setCard(head, sub) {
  $("cardHead").textContent = head;
  $("cardSub").textContent = sub;
}

function renderBridge(state) {
  const pill = $("bridge");
  const dot = $("dot");
  if (state.bridgeOk) {
    const discord = state.status && state.status.discord;
    pill.textContent = discord ? "connected" : "no discord";
    pill.className = discord ? "pill on" : "pill bad";
    dot.className = discord ? "dot on" : "dot";
  } else {
    pill.textContent = "bridge off";
    pill.className = "pill bad";
    dot.className = "dot";
  }

  const watching = state.status && state.status.watching;
  if (watching) {
    const [head, ...rest] = String(watching).split(" | ");
    setCard(head, rest.join(" · ") || "playing");
    $("state").textContent = "publishing";
    $("state").className = "state on";
  } else if (!state.bridgeOk) {
    setCard("Bridge not running", `Start it: python nowwatching.py`);
    $("state").textContent = "idle";
    $("state").className = "state";
  } else {
    setCard("Nothing playing", "Start something on an enabled site");
    $("state").textContent = "idle";
    $("state").className = "state";
  }
}

function renderSites(state) {
  const box = $("chips");
  box.textContent = "";
  if (!state.sites.length) {
    const e = document.createElement("span");
    e.className = "empty";
    e.textContent = "none yet";
    box.appendChild(e);
    return;
  }
  for (const host of state.sites) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createTextNode(host));
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = `Stop watching ${host}`;
    x.addEventListener("click", async () => {
      await ask({ type: "nw:disable", host });
      refresh();
    });
    chip.appendChild(x);
    box.appendChild(chip);
  }
}

function renderHere(state) {
  const btn = $("toggleSite");
  const hint = $("hereHint");
  $("host").textContent = current.host || "not a web page";

  if (!current.host) {
    btn.hidden = true;
    hint.textContent = "Open a streaming site, then reopen this popup.";
    return;
  }
  btn.hidden = false;

  const on = state.sites.some(
    (s) => current.host === s || current.host.endsWith(`.${s}`)
  );
  btn.textContent = on ? "Enabled" : "Enable";
  btn.className = on ? "btn on" : "btn";
  hint.textContent = on
    ? "Reload the page if this was just switched on."
    : "Grants access to this domain only. Mirrors need enabling once each.";
}

async function refresh() {
  const state = await ask({ type: "nw:state" });
  if (!state) return;
  $("probe").checked = !!state.probeFrames;
  $("port").value = state.port;
  renderBridge(state);
  renderSites(state);
  renderHere(state);
}

async function onEnableClick() {
  const state = await ask({ type: "nw:state" });
  const already =
    state &&
    state.sites.some(
      (s) => current.host === s || current.host.endsWith(`.${s}`)
    );
  if (already) {
    await ask({ type: "nw:disable", host: current.host });
    refresh();
    return;
  }

  const prep = await ask({ type: "nw:prepare", url: current.url });
  if (!prep || !prep.ok) {
    $("hereHint").textContent = (prep && prep.error) || "cannot enable this page";
    return;
  }
  // This call is the reason the popup exists: it must sit directly under a
  // click, with no awaits between the gesture and the request in older Chrome
  // builds. Chrome tolerates the awaits above; Firefox is stricter, so keep
  // this the last thing the handler does.
  let granted = false;
  try {
    granted = await chrome.permissions.request({ origins: prep.origins });
  } catch (e) {
    $("hereHint").textContent = String(e);
    return;
  }
  if (!granted) {
    $("hereHint").textContent = "Permission declined, so nothing changed.";
    return;
  }
  await ask({ type: "nw:enable-granted", url: current.url, host: prep.host });
  refresh();
}

async function onProbeChange(e) {
  const on = e.target.checked;
  if (on) {
    let granted = false;
    try {
      granted = await chrome.permissions.request({ origins: ["*://*/*"] });
    } catch {
      granted = false;
    }
    if (!granted) {
      e.target.checked = false;
      return;
    }
  } else {
    // Actually give the grant back when switched off. Leaving an all-sites
    // permission sitting there after the user turned the feature off would be
    // the kind of thing that makes people uninstall an extension.
    try {
      await chrome.permissions.remove({ origins: ["*://*/*"] });
    } catch {
      /* the site list still governs behaviour either way */
    }
  }
  await ask({ type: "nw:probe-granted", on });
  refresh();
}

async function onInspect() {
  const out = $("out");
  out.hidden = false;
  if (current.tabId === null) {
    out.textContent = "no tab";
    return;
  }
  chrome.tabs.sendMessage(
    current.tabId,
    { type: "nw:inspect" },
    { frameId: 0 },
    (reply) => {
      void chrome.runtime.lastError;
      out.textContent = reply
        ? JSON.stringify(reply, null, 1)
        : "No collector in this page.\n\nEither the site is not enabled yet, or\nit was enabled after the page loaded.\nEnable it, then reload the page.";
    }
  );
}

async function init() {
  $("repo").href = REPO;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    current.tabId = tab.id;
    current.url = tab.url || null;
    try {
      current.host = new URL(tab.url).hostname.replace(/^www\./, "");
    } catch {
      current.host = null;
    }
    // chrome:// and extension pages can never be enabled.
    if (current.url && !/^https?:/i.test(current.url)) current.host = null;
  }

  $("toggleSite").addEventListener("click", onEnableClick);
  $("probe").addEventListener("change", onProbeChange);
  $("inspect").addEventListener("click", onInspect);
  $("port").addEventListener("change", async (e) => {
    await ask({ type: "nw:port", port: Number(e.target.value) });
    refresh();
  });

  refresh();
  // The card is live while the popup is open, so a paused/played change shows
  // up without reopening it.
  setInterval(refresh, 2000);
}

document.addEventListener("DOMContentLoaded", init);
