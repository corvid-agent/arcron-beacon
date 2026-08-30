/* BEACON — randomness board. Reads the app's global state from the TestNet
   indexer once appId > 0 in deploy.json. TestNet only. Read-only. No wallet. */
(() => {
  const INDEXER = "https://testnet-idx.algonode.cloud";
  const ALGOD = "https://testnet-api.algonode.cloud";
  const EXPLORER = "https://testnet.explorer.perawallet.app/application/";
  const CONTRACT_SRC =
    "https://github.com/corvid-agent/arcron-beacon/blob/main/smart_contracts/beacon/contract.py";
  const DEFAULT_KEEPER = 769891898;
  const REFRESH_MS = 30000;

  function b64utf8(b64) {
    try { return atob(b64); } catch { return ""; }
  }

  function b64ToHex(b64) {
    try {
      const bin = atob(b64);
      let hex = "";
      for (let i = 0; i < bin.length; i++) {
        hex += bin.charCodeAt(i).toString(16).padStart(2, "0");
      }
      return hex;
    } catch {
      return "";
    }
  }

  function readGlobal(state, name) {
    if (!Array.isArray(state)) return null;
    for (const kv of state) {
      if (b64utf8(kv.key) !== name) continue;
      if (kv.value && kv.value.type === 2) return { kind: "uint", v: kv.value.uint };
      if (kv.value && kv.value.type === 1) return { kind: "bytes", v: kv.value.bytes };
      return null;
    }
    return null;
  }

  async function fetchJson(url, noStore) {
    const opts = { headers: { Accept: "application/json" } };
    if (noStore) opts.cache = "no-store";
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(url + " " + res.status);
    return res.json();
  }

  function flaps(el, text) {
    el.replaceChildren();
    for (const ch of String(text)) {
      const d = document.createElement("span");
      d.className = "flap" + (ch === " " ? " blank" : "");
      d.textContent = ch === " " ? " " : ch;
      el.appendChild(d);
    }
  }

  function setStatus(word, cls, subHtml) {
    const el = document.getElementById("status");
    el.className = "flaps big " + cls;
    flaps(el, word.toUpperCase());
    document.getElementById("subhead").innerHTML = subHtml;
    document.title = "BEACON — " + word.toUpperCase();
  }

  const STAT_IDS = [
    "stat-round", "stat-seed", "stat-reveals",
    "stat-target", "stat-chain", "stat-keeper",
  ];

  function fillStats(map) {
    for (const id of STAT_IDS) {
      flaps(document.getElementById(id), map[id] || "—");
    }
  }

  function shortHex(hex) {
    if (!hex) return "—";
    return hex.length > 18 ? hex.slice(0, 8) + "…" + hex.slice(-8) : hex;
  }

  let cfgPromise = null;
  function loadConfig() {
    if (!cfgPromise) {
      cfgPromise = fetchJson("./deploy.json", true).then((c) => ({
        appId: Number(c.appId) || 0,
        keeper: Number(c.keeperAppId) || DEFAULT_KEEPER,
        network: c.network || "testnet",
        notes: c.notes || "",
      }));
    }
    return cfgPromise;
  }

  async function tick() {
    let cfg;
    try {
      cfg = await loadConfig();
    } catch (e) {
      setStatus("FEED DOWN", "down",
        "deploy.json unreadable · showing nothing rather than guessing");
      fillStats({});
      return;
    }
    document.getElementById("keeper-meta").textContent =
      cfg.network + " · Arcron keeper " + cfg.keeper;

    if (cfg.appId <= 0) {
      setStatus("NOT DEPLOYED", "gate",
        'contract exists as <a href="' + CONTRACT_SRC + '">source</a> only' +
        " · lights up after TestNet deploy + set_keeper + Arcron registration");
      fillStats({ "stat-keeper": String(cfg.keeper) });
      return;
    }

    let round, gs;
    try {
      const status = await fetchJson(ALGOD + "/v2/status");
      round = status["last-round"];
      const app = await fetchJson(INDEXER + "/v2/applications/" + cfg.appId);
      const params = (app.application && app.application.params) || app.params || {};
      gs = params["global-state"];
    } catch (e) {
      setStatus("FEED DOWN", "down",
        "indexer unreachable · showing nothing rather than guessing");
      fillStats({ "stat-chain": round == null ? "—" : String(round) });
      return;
    }

    const revealedRound = readGlobal(gs, "revealed_round");
    const revealedSeed = readGlobal(gs, "revealed_seed");
    const reveals = readGlobal(gs, "reveals");
    const target = readGlobal(gs, "target_round");
    const keeperApp = readGlobal(gs, "keeper_app");

    const nReveals = reveals && reveals.kind === "uint" ? reveals.v : 0;
    const seedHex = revealedSeed && revealedSeed.kind === "bytes"
      ? b64ToHex(revealedSeed.v) : "";

    fillStats({
      "stat-round": revealedRound ? String(revealedRound.v) : "—",
      "stat-seed": shortHex(seedHex),
      "stat-reveals": String(nReveals),
      "stat-target": target && target.v > 0 ? String(target.v) : "planning",
      "stat-chain": String(round),
      "stat-keeper": keeperApp ? String(keeperApp.v) : "—",
    });

    if (!keeperApp || keeperApp.v === 0) {
      setStatus("NO KEEPER", "gate",
        'app <a href="' + EXPLORER + cfg.appId + '">' + cfg.appId + "</a>" +
        " is live but set_keeper has not run yet");
      return;
    }

    if (nReveals === 0) {
      setStatus("SEEKING", "seeking",
        "keeper wired · waiting for the first future round to pass" +
        (target && target.v > 0 ? " · committed to round " + target.v : ""));
      return;
    }

    setStatus("LIVE", "live",
      'app <a href="' + EXPLORER + cfg.appId + '">' + cfg.appId + "</a>" +
      " · latest seed from round " + (revealedRound ? revealedRound.v : "?") +
      " · verify on any TestNet explorer");
  }

  tick();
  setInterval(tick, REFRESH_MS);
})();
