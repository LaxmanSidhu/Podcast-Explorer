/* =========================================================================
   Podcast Explorer - frontend logic
   ========================================================================= */
const App = (() => {
  "use strict";

  // ------------------------------------------------------------ state
  let podcasts = [];          // processed podcast cards
  let currentFeed = null;     // { feed, episodes } for the open podcast
  let currentEpisodes = [];   // filtered/rendered episodes
  let clip = null;            // { audioUrl, title, total } for the clip modal
  let clipPreset = null;      // active preset id or null (=> custom range)

  let selectedEps = new Set();   // selected episode indices in the open podcast
  let selectedPods = new Set();  // selected podcast card indices
  let bulkJob = null;            // active bulk download job id
  let bulkTimer = null;          // progress poll timer
  let bulkCancelled = false;

  const BULK_MAX = 200;          // keep in sync with bulk.MAX_FILES on the server

  const EXAMPLE_URLS = [
    "https://podcasts.apple.com/in/podcast/the-joe-rogan-experience/id360084272",
    "https://podcasts.apple.com/us/podcast/this-past-weekend-w-theo-von/id1190981360",
    "https://podcasts.apple.com/in/podcast/build-with-malav/id1896648686",
    "https://podcasts.apple.com/in/podcast/raat-wali-kahani-horror-stories/id1896748831",
  ];

  // ------------------------------------------------------------ tiny helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function fmtTime(sec) {
    if (sec == null || isNaN(sec)) return "--:--";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
  }

  function parseTime(str) {
    if (str == null || str === "") return null;
    str = String(str).trim();
    if (str.includes(":")) {
      const parts = str.split(":").map(Number);
      if (parts.some(isNaN)) return null;
      return parts.reduce((acc, p) => acc * 60 + p, 0);
    }
    const n = Number(str);
    return isNaN(n) ? null : n;
  }

  const ICON = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    rss: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1.5" fill="currentColor" stroke="none"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>',
    scissors: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.12 15.88M14.47 14.48 20 20M8.12 8.12 12 12"/></svg>',
    star: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',
  };

  // ------------------------------------------------------------ toasts
  function toast(msg, type = "ok") {
    const host = $("#toastHost");
    const el = document.createElement("div");
    el.className = "toast " + (type === "err" ? "err" : "ok");
    el.innerHTML = (type === "err" ? ICON.warn : ICON.check) + "<span>" + esc(msg) + "</span>";
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateY(10px)";
      setTimeout(() => el.remove(), 300);
    }, type === "err" ? 4200 : 2600);
  }

  // ------------------------------------------------------------ tabs / input
  function tab(name) {
    $$(".seg button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tabpane").forEach((p) => p.classList.toggle("active", p.dataset.pane === name));
  }

  function initFileInput() {
    const dz = $("#dropzone");
    const input = $("#fileInput");
    if (!dz) return;
    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", () => showChosen(input.files[0]));
    ["dragover", "dragenter"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        showChosen(input.files[0]);
      }
    });
  }

  function showChosen(file) {
    const el = $("#fileChosen");
    if (file) { el.style.display = "block"; el.textContent = "Selected: " + file.name; }
    else { el.style.display = "none"; }
  }

  function loadSample() {
    tab("paste");
    $("#urls").value = EXAMPLE_URLS.join("\n");
    toast("Example URLs loaded");
  }

  // ------------------------------------------------------------ naming helpers
  // Episodes display newest-first. The newest of N episodes is "Ep{N}", the
  // oldest is "Ep1". ep.index is 1 for the newest, so EpNumber = total - index + 1.
  function epNumber(ep, total) {
    const n = total - (ep.index - 1);
    return n > 0 ? n : ep.index;
  }

  function audioExt(ep) {
    const u = (ep.audio_url || "").split("?")[0].toLowerCase();
    const m = u.match(/\.(mp3|m4a|mp4|aac|ogg|oga|wav|flac)$/);
    if (m) return m[1] === "mp4" ? "m4a" : (m[1] === "oga" ? "ogg" : m[1]);
    const t = (ep.audio_type || "").toLowerCase();
    if (t.includes("mp4") || t.includes("m4a")) return "m4a";
    if (t.includes("aac")) return "aac";
    if (t.includes("ogg")) return "ogg";
    if (t.includes("wav")) return "wav";
    return "mp3";
  }

  // Keep the title as close to the original as filesystems allow: preserve
  // spaces and legal punctuation (apostrophes, #, commas, &, ...); only handle
  // the characters Windows forbids ( : / \ | < > " ? * ).
  function cleanName(s) {
    return String(s || "")
      .replace(/[/\\|]/g, "-")
      .replace(/:/g, " - ")
      .replace(/[<>"?*\x00-\x1f]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\.+$/, "")
      .trim();
  }

  function episodeFileName(ep, total) {
    const title = cleanName(ep.title || "Untitled") || "Untitled";
    return "Ep" + epNumber(ep, total) + " - " + title + "." + audioExt(ep);
  }

  // ------------------------------------------------------------ process
  async function process() {
    const text = $("#urls").value.trim();
    const file = $("#fileInput").files[0];
    if (!text && !file) {
      toast("Paste at least one Apple Podcast URL", "err");
      return;
    }

    const btn = $("#fetchBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span>Fetching...</span>';
    $("#processNotice").innerHTML = "";
    showSkeletons();

    const fd = new FormData();
    fd.append("input", text);
    if (file) fd.append("file", file);

    try {
      const res = await fetch("/api/process", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Something went wrong");

      podcasts = data.podcasts || [];
      renderCards(podcasts, data.failed || []);
    } catch (err) {
      $("#resultsWrap").style.display = "none";
      $("#processNotice").innerHTML =
        '<div class="notice notice-error">' + ICON.warn + "<div>" + esc(err.message) + "</div></div>";
    } finally {
      btn.disabled = false;
      btn.innerHTML = ICON.search + "<span>Fetch podcasts</span>";
    }
  }

  function showSkeletons() {
    $("#resultsWrap").style.display = "block";
    $("#resultCount").textContent = "";
    $("#cardGrid").innerHTML = Array.from({ length: 4 }).map(() =>
      '<div class="pcard skcard"><div class="skel r" style="width:60%"></div>' +
      '<div class="skel r" style="width:90%"></div><div class="skel r" style="width:40%"></div>' +
      '<div class="skel r" style="height:44px;margin-top:14px"></div></div>').join("");
  }

  function renderCards(list, failed) {
    $("#resultsWrap").style.display = "block";
    const n = list.length;
    $("#resultCount").textContent = n + (n === 1 ? " podcast" : " podcasts");

    let noticeHTML = "";
    if (failed && failed.length) {
      noticeHTML = '<div class="notice notice-warn">' + ICON.warn +
        "<div><strong>" + failed.length + " input" + (failed.length > 1 ? "s" : "") +
        " could not be processed.</strong> " +
        esc(failed.map((f) => f.input).join(", ")) + "</div></div>";
    }
    $("#processNotice").innerHTML = noticeHTML;

    if (!n) {
      $("#cardGrid").innerHTML =
        '<div class="empty" style="grid-column:1/-1">' + ICON.grid +
        "<h3>No podcasts found</h3><p>Check the URLs and try again.</p></div>";
      return;
    }

    $("#cardGrid").innerHTML = list.map((p, i) => cardHTML(p, i)).join("");
    selectedPods = new Set();
    renderPodToolbar();
    // Auto-open when there is exactly one result.
    if (n === 1) viewEpisodes(0);
  }

  // ------------------------------------------------------------ podcast selection
  function renderPodToolbar() {
    const bar = $("#podToolbar");
    const selectable = podcasts.filter((p) => p.has_rss).length;
    if (!bar) return;
    if (podcasts.length < 2 || selectable === 0) { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    bar.innerHTML =
      '<label class="selectall"><input type="checkbox" id="podSelectAll" onchange="App.selectAllPods(this.checked)">' +
      '<span class="cbox">' + ICON.check + "</span> Select all</label>" +
      '<span class="sel-hint" id="podSelHint">Choose podcasts to download together.</span>' +
      '<span class="spacer"></span>' +
      '<button class="btn btn-primary btn-sm" id="podDownloadBtn" disabled onclick="App.downloadPods()">' +
      ICON.download + ' <span>Download episodes</span></button>';
    updatePodSelectionUI();
  }

  function togglePod(i, on) {
    if (on) selectedPods.add(i); else selectedPods.delete(i);
    const card = $('[data-pod-card="' + i + '"]');
    if (card) card.classList.toggle("selected", on);
    updatePodSelectionUI();
  }

  function selectAllPods(on) {
    selectedPods = new Set();
    podcasts.forEach((p, i) => { if (p.has_rss && on) selectedPods.add(i); });
    $$('input[data-pod]').forEach((cb) => {
      cb.checked = on;
      const card = cb.closest(".pcard");
      if (card) card.classList.toggle("selected", on);
    });
    updatePodSelectionUI();
  }

  function updatePodSelectionUI() {
    const n = selectedPods.size;
    const btn = $("#podDownloadBtn");
    const hint = $("#podSelHint");
    const all = $("#podSelectAll");
    const selectable = podcasts.filter((p) => p.has_rss).length;
    if (btn) {
      btn.disabled = n === 0;
      btn.querySelector("span").textContent =
        n === 0 ? "Download episodes" : "Download " + n + " podcast" + (n > 1 ? "s" : "");
    }
    if (hint) {
      hint.textContent = n === 0
        ? "Choose podcasts to download together."
        : n + " selected \u00b7 one folder per podcast in the zip";
    }
    if (all) all.checked = n > 0 && n === selectable;
  }

  function cardHTML(p, i) {
    const art = p.artwork
      ? '<img class="pcard-art" src="' + esc(p.artwork) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
      : '<div class="pcard-art placeholder">' + ICON.mic + "</div>";
    const genre = p.primary_genre
      ? '<span class="pcard-genre">' + esc(p.primary_genre) + "</span>" : "";
    const epCount = p.episode_count !== "" ? p.episode_count : "&mdash;";
    const rssPill = p.has_rss
      ? '<span class="pill pill-ok">' + ICON.rss + " RSS</span>"
      : '<span class="pill pill-no">No RSS</span>';

    const checkbox = p.has_rss
      ? '<label class="pcard-check" onclick="event.stopPropagation()">' +
          '<input type="checkbox" data-pod="' + i + '" onchange="App.togglePod(' + i + ', this.checked)">' +
          '<span class="cbox">' + ICON.check + "</span></label>"
      : "";

    return (
      '<div class="pcard" data-pod-card="' + i + '" onclick="App.viewEpisodes(' + i + ')">' +
        checkbox +
        '<div class="pcard-top">' + art +
          '<div class="pcard-meta">' +
            '<div class="pcard-title">' + esc(p.podcast_name || "Untitled podcast") + "</div>" +
            '<div class="pcard-pub">' + esc(p.artist_name || "Unknown publisher") + "</div>" +
            genre +
          "</div>" +
        "</div>" +
        '<div class="pcard-stats">' +
          "<span><b>" + epCount + "</b> episodes</span>" +
          (p.release_date ? "<span>Since <b>" + esc(p.release_date.slice(0, 4)) + "</b></span>" : "") +
        "</div>" +
        '<div class="pcard-foot">' + rssPill +
          '<span class="go">View episodes ' + ICON.arrow + "</span>" +
        "</div>" +
      "</div>"
    );
  }

  // ------------------------------------------------------------ episodes view
  async function viewEpisodes(index) {
    const p = podcasts[index];
    if (!p) return;
    if (!p.feed_url) { toast("This podcast has no RSS feed available", "err"); return; }

    switchView("episodes-view");
    window.scrollTo({ top: 0 });
    $("#showHeader").innerHTML = headerSkeleton(p);
    $("#episodesArea").innerHTML =
      '<div class="empty">' + '<span class="spinner dark" style="width:26px;height:26px;margin:0 auto 14px;display:block"></span>' +
      "<p>Loading episodes from the RSS feed...</p></div>";

    try {
      const res = await fetch("/api/episodes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ feed_url: p.feed_url, apple_url: p.apple_url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not load episodes");

      currentFeed = data;
      currentFeed._card = p;
      currentEpisodes = data.episodes;
      renderShowHeader(p, data.feed);
      renderEpisodes(data.episodes, p.focus_audio_url);
    } catch (err) {
      $("#episodesArea").innerHTML =
        '<div class="notice notice-error">' + ICON.warn + "<div>" + esc(err.message) + "</div></div>";
    }
  }

  function headerSkeleton(p) {
    return '<div class="show-header"><div class="show-art skel"></div>' +
      '<div class="show-info" style="flex:1">' +
      '<div class="skel r" style="height:26px;width:50%;margin-bottom:10px"></div>' +
      '<div class="skel r" style="height:14px;width:30%;margin-bottom:14px"></div>' +
      '<div class="skel r" style="height:12px;width:80%"></div></div></div>';
  }

  function renderShowHeader(p, feed) {
    const art = (feed.image || p.artwork)
      ? '<img class="show-art" src="' + esc(feed.image || p.artwork) + '" alt="" onerror="this.style.display=\'none\'">'
      : '<div class="show-art"></div>';

    const chips = [];
    if (p.primary_genre) chips.push('<span class="chip">' + esc(p.primary_genre) + "</span>");
    if (feed.rating) chips.push('<span class="chip star">' + ICON.star + " " + esc(feed.rating) +
      (feed.review_count ? " (" + Number(feed.review_count).toLocaleString() + ")" : "") + "</span>");
    if (p.country) chips.push('<span class="chip">' + esc(p.country.toUpperCase()) + "</span>");
    if (feed.episode_count) chips.push('<span class="chip">' + ICON.mic + " " + feed.episode_count + " episodes</span>");

    $("#showHeader").innerHTML =
      '<div class="show-header">' + art +
        '<div class="show-info">' +
          "<h1>" + esc(feed.title || p.podcast_name) + "</h1>" +
          '<div class="show-pub">by <b>' + esc(feed.author || p.artist_name || "Unknown") + "</b></div>" +
          (feed.description ? '<p class="show-desc">' + esc(feed.description) + "</p>" : "") +
          '<div class="show-chips">' + chips.join("") + "</div>" +
          '<div class="show-actions">' +
            '<button class="btn btn-soft btn-sm" onclick="App.copyRss()">' + ICON.copy + " Copy RSS URL</button>" +
            '<button class="btn btn-ghost btn-sm" onclick="App.viewRss()">' + ICON.eye + " View RSS</button>" +
            '<button class="btn btn-ghost btn-sm" onclick="App.downloadRss()">' + ICON.download + " Download RSS</button>" +
          "</div>" +
        "</div>" +
      "</div>";
  }

  function renderEpisodes(eps, focusAudio) {
    selectedEps = new Set();
    if (!eps.length) {
      $("#episodesArea").innerHTML =
        '<div class="empty">' + ICON.mic + "<h3>No episodes</h3><p>This feed did not return any episodes.</p></div>";
      return;
    }

    const total = eps.length;
    const withAudio = eps.filter((e) => e.audio_url).length;
    const rows = eps.map((ep) => episodeRow(ep, focusAudio, total)).join("");

    const presetChips =
      '<button class="chip-btn" onclick="App.selectFirst(5)">First 5</button>' +
      '<button class="chip-btn" onclick="App.selectFirst(10)">First 10</button>' +
      '<button class="chip-btn" onclick="App.selectFirst(25)">First 25</button>' +
      '<button class="chip-btn" onclick="App.selectFirst(0)">All</button>' +
      '<button class="chip-btn" onclick="App.selectFirst(-1)">None</button>';

    $("#episodesArea").innerHTML =
      '<div class="episodes-toolbar">' +
        "<h2>Episodes</h2>" +
        '<div class="search-box">' + ICON.search +
          '<input type="text" id="epSearch" placeholder="Filter episodes..." oninput="App.filterEpisodes()">' +
        "</div>" +
      "</div>" +
      '<div class="bulk-toolbar ep-bulk">' +
        '<label class="selectall"><input type="checkbox" id="epSelectAll" onchange="App.selectAllEps(this.checked)">' +
          '<span class="cbox">' + ICON.check + "</span> Select all</label>" +
        '<span class="chip-btns">' + presetChips + "</span>" +
        '<span class="sel-hint" id="epSelHint">' + withAudio + " downloadable episode" + (withAudio === 1 ? "" : "s") + "</span>" +
        '<span class="spacer"></span>' +
        '<button class="btn btn-primary btn-sm" id="epDownloadBtn" disabled onclick="App.downloadEps()">' +
          ICON.download + ' <span>Download selected</span></button>' +
      "</div>" +
      '<div class="ep-table-wrap"><table class="episodes"><thead><tr>' +
        '<th class="col-check"></th><th>#</th><th>Episode</th><th>Length</th><th>Published</th><th>Player</th><th>Download</th>' +
      "</tr></thead><tbody id='epBody'>" + rows + "</tbody></table></div>";

    updateEpSelectionUI();

    // Scroll to the focused episode (when opened from an episode URL).
    if (focusAudio) {
      const row = $('tr[data-audio="' + cssEsc(focusAudio) + '"]');
      if (row) setTimeout(() => row.scrollIntoView({ behavior: "smooth", block: "center" }), 200);
    }
  }

  function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

  function episodeRow(ep, focusAudio, total) {
    const focused = focusAudio && ep.audio_url === focusAudio;
    const desc = ep.description || "";
    const player = ep.audio_url
      ? '<audio controls preload="none" src="' + esc(ep.audio_url) + '"></audio>'
      : '<span class="noaudio">No audio</span>';

    const epNo = epNumber(ep, total);

    let actions = "";
    if (ep.audio_url) {
      actions =
        '<div class="ep-actions">' +
          '<button class="btn btn-soft btn-sm" onclick="App.openClip(' + JSON.stringify(ep.index) + ')">' +
            ICON.scissors + " Clip</button>" +
          '<button class="btn btn-ghost btn-sm" onclick="App.downloadOne(' + JSON.stringify(ep.index) + ')">' +
            ICON.download + " Full</button>" +
        "</div>";
    } else {
      actions = '<span class="noaudio">&mdash;</span>';
    }

    const descBlock = desc
      ? '<div class="ep-desc" id="d' + ep.index + '">' + esc(desc) + "</div>" +
        (desc.length > 140 ? '<span class="ep-more" onclick="App.toggleDesc(' + ep.index + ')">Show more</span>' : "")
      : "";

    const check = ep.audio_url
      ? '<label class="ep-check" onclick="event.stopPropagation()">' +
          '<input type="checkbox" data-ep="' + ep.index + '" onchange="App.toggleEp(' + ep.index + ', this.checked)">' +
          '<span class="cbox">' + ICON.check + "</span></label>"
      : "";

    return (
      '<tr data-audio="' + esc(ep.audio_url || "") + '" data-title="' +
        esc((ep.title || "").toLowerCase()) + '"' + (focused ? ' class="focus-row"' : "") + ">" +
        '<td class="col-check">' + check + "</td>" +
        '<td class="ep-num">' + epNo + "</td>" +
        '<td class="ep-main"><div class="ep-title">' + esc(ep.title || "Untitled") + "</div>" + descBlock + "</td>" +
        '<td class="ep-meta-cell"><span class="ep-dur">' + (ep.duration || "&mdash;") + "</span></td>" +
        '<td class="ep-meta-cell"><span class="ep-date">' + esc(ep.published_iso || ep.published || "") + "</span></td>" +
        '<td class="ep-audio">' + player + "</td>" +
        "<td>" + actions + "</td>" +
      "</tr>"
    );
  }

  // ------------------------------------------------------------ episode selection
  function toggleEp(index, on) {
    if (on) selectedEps.add(index); else selectedEps.delete(index);
    updateEpSelectionUI();
  }

  function selectAllEps(on) {
    selectedEps = new Set();
    if (on) currentEpisodes.forEach((e) => { if (e.audio_url) selectedEps.add(e.index); });
    syncEpCheckboxes();
    updateEpSelectionUI();
  }

  // Select the first N episodes as displayed (newest first). 0 = all, -1 = none.
  function selectFirst(n) {
    selectedEps = new Set();
    const withAudio = currentEpisodes.filter((e) => e.audio_url);
    let take = withAudio;
    if (n === -1) take = [];
    else if (n > 0) take = withAudio.slice(0, n);
    take.forEach((e) => selectedEps.add(e.index));
    syncEpCheckboxes();
    updateEpSelectionUI();
    if (n > 0 && withAudio.length < n) {
      toast("Only " + withAudio.length + " downloadable episode" + (withAudio.length === 1 ? "" : "s") + " available");
    }
  }

  function syncEpCheckboxes() {
    $$('input[data-ep]').forEach((cb) => {
      cb.checked = selectedEps.has(Number(cb.dataset.ep));
    });
  }

  function updateEpSelectionUI() {
    const n = selectedEps.size;
    const btn = $("#epDownloadBtn");
    const all = $("#epSelectAll");
    const withAudio = currentEpisodes.filter((e) => e.audio_url).length;
    if (btn) {
      btn.disabled = n === 0;
      btn.querySelector("span").textContent =
        n === 0 ? "Download selected" : "Download " + n + " episode" + (n > 1 ? "s" : "");
    }
    if (all) all.checked = n > 0 && n === withAudio;
  }

  function toggleDesc(i) {
    const el = $("#d" + i);
    const more = el.nextElementSibling;
    el.classList.toggle("open");
    if (more) more.textContent = el.classList.contains("open") ? "Show less" : "Show more";
  }

  function filterEpisodes() {
    const q = $("#epSearch").value.trim().toLowerCase();
    $$("#epBody tr").forEach((tr) => {
      tr.style.display = !q || tr.dataset.title.includes(q) ? "" : "none";
    });
  }

  // ------------------------------------------------------------ RSS actions
  function copyRss() {
    const url = currentFeed && currentFeed.feed.rss_url;
    if (!url) return;
    navigator.clipboard.writeText(url)
      .then(() => toast("RSS URL copied to clipboard"))
      .catch(() => toast("Could not copy", "err"));
  }
  function viewRss() {
    const url = currentFeed && currentFeed.feed.rss_url;
    if (url) window.open("/api/rss?feed_url=" + encodeURIComponent(url), "_blank");
  }
  function downloadRss() {
    const url = currentFeed && currentFeed.feed.rss_url;
    if (url) { window.location.href = "/api/rss?feed_url=" + encodeURIComponent(url) + "&download=1"; toast("Downloading RSS XML"); }
  }
  function notifyDownload() { toast("Starting download..."); }

  // ------------------------------------------------------------ clip modal
  function openClip(index) {
    const ep = currentEpisodes.find((e) => e.index === index);
    if (!ep || !ep.audio_url) return;
    clip = { audioUrl: ep.audio_url, title: ep.title || "clip", total: ep.duration_seconds || null };
    clipPreset = null;

    $("#clipEpName").textContent = ep.title || "";
    $("#clipError").classList.remove("show");
    $("#clipStart").value = "";
    $("#clipEnd").value = "";
    renderPresets();
    renderTimeline();

    $("#clipModal").classList.add("open");
    document.body.style.overflow = "hidden";
  }

  function renderPresets() {
    const hasTotal = clip.total && clip.total > 0;
    const presets = [
      { id: "first_2", name: "First 2 minutes", hint: "0:00 - 2:00", ok: true },
      { id: "first_5", name: "First 5 minutes", hint: "0:00 - 5:00", ok: true },
      { id: "last_5", name: "Last 5 minutes", hint: hasTotal ? fmtTime(clip.total - 300) + " - " + fmtTime(clip.total) : "needs length", ok: hasTotal },
      { id: "last_10", name: "Last 10 minutes", hint: hasTotal ? fmtTime(clip.total - 600) + " - " + fmtTime(clip.total) : "needs length", ok: hasTotal },
    ];
    $("#presetGrid").innerHTML = presets.map((p) =>
      '<button class="preset' + (clipPreset === p.id ? " active" : "") + '"' +
      (p.ok ? "" : " disabled") +
      ' onclick="App.pickPreset(\'' + p.id + '\')">' +
      '<div class="p-name">' + p.name + "</div>" +
      '<div class="p-hint">' + p.hint + "</div></button>").join("");
  }

  function pickPreset(id) {
    clipPreset = id;
    // Reflect the preset in the range inputs so the window is visible.
    let start = 0, end = 0;
    if (id === "first_2") { start = 0; end = 120; }
    else if (id === "first_5") { start = 0; end = 300; }
    else if (id === "last_5") { start = clip.total - 300; end = clip.total; }
    else if (id === "last_10") { start = clip.total - 600; end = clip.total; }
    $("#clipStart").value = fmtTime(start);
    $("#clipEnd").value = fmtTime(end);
    $("#clipError").classList.remove("show");
    renderPresets();
    renderTimeline(start, end);
  }

  function onRangeInput() {
    // Editing the fields switches to custom mode.
    clipPreset = null;
    renderPresets();
    const s = parseTime($("#clipStart").value);
    const e = parseTime($("#clipEnd").value);
    renderTimeline(s, e);
  }

  function renderTimeline(start, end) {
    const total = clip.total;
    $("#tlEnd").textContent = total ? fmtTime(total) : "--:--";
    const fill = $("#tlFill");
    const cap = $("#tlCaption");

    if (start == null && $("#clipStart").value) start = parseTime($("#clipStart").value);
    if (end == null && $("#clipEnd").value) end = parseTime($("#clipEnd").value);

    if (start == null || end == null || end <= start) {
      fill.style.left = "0"; fill.style.width = "0";
      cap.innerHTML = "Select a preset or enter a start and end time.";
      return;
    }
    const len = end - start;
    if (total && total > 0) {
      fill.style.left = Math.max(0, Math.min(100, (start / total) * 100)) + "%";
      fill.style.width = Math.max(1.5, Math.min(100, (len / total) * 100)) + "%";
    } else {
      // Unknown total: show the window anchored near the start.
      fill.style.left = "4%"; fill.style.width = "40%";
    }
    cap.innerHTML = "Clip <b>" + fmtTime(start) + "</b> to <b>" + fmtTime(end) +
      "</b> &middot; length <b>" + fmtTime(len) + "</b>";
  }

  function closeClip() {
    $("#clipModal").classList.remove("open");
    document.body.style.overflow = "";
  }

  async function downloadClip() {
    if (!clip) return;
    const params = new URLSearchParams();
    params.set("audio_url", clip.audioUrl);
    params.set("title", clip.title);

    if (clipPreset) {
      params.set("mode", clipPreset);
      if (clip.total) params.set("total", String(clip.total));
    } else {
      const s = parseTime($("#clipStart").value);
      const e = parseTime($("#clipEnd").value);
      if (s == null || e == null) { return clipErr("Enter both a start and end time, or pick a preset."); }
      if (e <= s) { return clipErr("End time must be after start time."); }
      params.set("mode", "range");
      params.set("start", String(s));
      params.set("end", String(e));
    }

    const btn = $("#clipDownloadBtn");
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span>Cutting clip...</span>';
    $("#clipError").classList.remove("show");

    try {
      const res = await fetch("/api/clip?" + params.toString());
      if (!res.ok) {
        // Read the body exactly once, then try to interpret it as JSON.
        // (Reading it twice throws "body stream already read".)
        let msg = "Could not generate the clip.";
        const raw = await res.text();
        try {
          const j = JSON.parse(raw);
          if (j && j.error) msg = j.error;
        } catch (_) {
          if (raw) msg = raw.replace(/<[^>]+>/g, "").trim().slice(0, 200);
        }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^"]+)"?/);
      const name = m ? m[1] : "clip.mp3";
      triggerBlobDownload(blob, name);
      closeClip();
      toast("Clip downloaded");
    } catch (err) {
      clipErr(err.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  function clipErr(msg) {
    const el = $("#clipError");
    el.textContent = msg;
    el.classList.add("show");
  }

  function triggerBlobDownload(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // ------------------------------------------------------------ bulk download
  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + " " + u[i];
  }
  function shortName(p) {
    const parts = String(p).split("/");
    return parts[parts.length - 1];
  }

  function showProgress(title) {
    bulkCancelled = false;
    $("#progTitle").textContent = title || "Preparing your download";
    $("#progPhase").textContent = "";
    $("#progCurrent").textContent = "";
    $("#progNote").textContent = "";
    $("#progCount").textContent = "";
    $("#progCancelBtn").disabled = false;
    setBar(0, 0);
    $("#progressModal").classList.add("open");
    document.body.style.overflow = "hidden";
  }
  function setPhase(t) { $("#progPhase").textContent = t; }
  function setBar(done, total) {
    const fill = $("#progFill");
    const indeterminate = !total || total <= 0;
    fill.classList.toggle("indeterminate", indeterminate);
    const pct = indeterminate ? 0 : Math.round((done / total) * 100);
    fill.style.width = (indeterminate ? 30 : Math.max(2, pct)) + "%";
    $("#progPct").textContent = indeterminate ? "" : pct + "%";
    $("#progCount").textContent = indeterminate ? "" : (done + " / " + total + " files");
  }
  function hideProgress() {
    $("#progressModal").classList.remove("open");
    document.body.style.overflow = "";
  }

  // Single episode: runs through the same job/progress pipeline as bulk so the
  // user sees progress while the server fetches (and, if the host inserted ads,
  // strips them) instead of staring at a link that appears to hang.
  async function downloadOne(index) {
    const ep = currentEpisodes.find((e) => e.index === index);
    if (!ep || !ep.audio_url) { toast("No audio for this episode", "err"); return; }
    const total = currentEpisodes.length;
    const item = {
      url: ep.audio_url,
      path: episodeFileName(ep, total),
      duration: ep.duration_seconds || 0,
    };
    await runBulk([item], item.path, "Downloading episode", false, true);
  }

  async function downloadEps() {
    if (!currentFeed) return;
    const total = currentEpisodes.length;
    const chosen = currentEpisodes.filter((e) => selectedEps.has(e.index) && e.audio_url);
    if (!chosen.length) { toast("Select at least one episode", "err"); return; }
    if (chosen.length > BULK_MAX) {
      toast("Max " + BULK_MAX + " files per download (you selected " + chosen.length + ").", "err");
      return;
    }
    const items = chosen.map((e) => ({
      url: e.audio_url, path: episodeFileName(e, total), duration: e.duration_seconds || 0,
    }));
    const podName =
      cleanName((currentFeed.feed && currentFeed.feed.title) ||
        (currentFeed._card && currentFeed._card.podcast_name) || "podcast") || "podcast";
    const zipName = podName + " - " + chosen.length + " episodes.zip";
    await runBulk(items, zipName, "Downloading " + chosen.length + " episode" + (chosen.length > 1 ? "s" : ""), false);
  }

  async function downloadPods() {
    const chosen = Array.from(selectedPods).map((i) => podcasts[i]).filter((p) => p && p.feed_url);
    if (!chosen.length) { toast("Select at least one podcast", "err"); return; }

    showProgress("Preparing your download");
    setPhase("Loading episode lists\u2026");

    const items = [];
    const usedFolders = new Set();
    for (let i = 0; i < chosen.length; i++) {
      if (bulkCancelled) { hideProgress(); return; }
      const p = chosen[i];
      setPhase("Loading episodes " + (i + 1) + " of " + chosen.length +
        (p.podcast_name ? " \u2014 " + p.podcast_name : ""));
      let data;
      try {
        const res = await fetch("/api/episodes", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feed_url: p.feed_url, apple_url: p.apple_url }),
        });
        data = await res.json();
        if (!res.ok) throw new Error(data.error || "Could not load episodes");
      } catch (err) {
        toast((p.podcast_name || "A podcast") + ": " + err.message, "err");
        continue;
      }
      const eps = data.episodes || [];
      const total = eps.length;
      let folder = cleanName(p.podcast_name || (data.feed && data.feed.title) || "Podcast") || "Podcast";
      let unique = folder, k = 2;
      while (usedFolders.has(unique.toLowerCase())) unique = folder + " (" + (k++) + ")";
      usedFolders.add(unique.toLowerCase());
      eps.forEach((e) => {
        if (e.audio_url) items.push({
          url: e.audio_url, path: unique + "/" + episodeFileName(e, total),
          duration: e.duration_seconds || 0,
        });
      });
    }

    if (bulkCancelled) { hideProgress(); return; }
    if (!items.length) { hideProgress(); toast("No downloadable episodes found", "err"); return; }
    if (items.length > BULK_MAX) {
      hideProgress();
      toast("That would be " + items.length + " files; the max per download is " + BULK_MAX +
        ". Select fewer podcasts, or open a podcast to pick specific episodes.", "err");
      return;
    }

    const zipName = chosen.length === 1
      ? (cleanName(chosen[0].podcast_name) || "podcast") + " episodes.zip"
      : chosen.length + " podcasts - episodes.zip";
    await runBulk(items, zipName, "Downloading " + items.length + " files", true);
  }

  async function runBulk(items, zipName, phaseLabel, alreadyOpen, single) {
    if (!alreadyOpen) showProgress("Preparing your download");
    setPhase(phaseLabel || "Starting\u2026");
    setBar(0, 0);

    let start;
    try {
      const res = await fetch("/api/bulk/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, zip_name: zipName, single: !!single }),
      });
      start = await res.json();
      if (!res.ok) throw new Error(start.error || "Could not start the download");
    } catch (err) { hideProgress(); toast(err.message, "err"); return; }

    bulkJob = start.job_id;
    const total = start.total || items.length;
    setPhase("Starting\u2026");
    setBar(0, 0);
    pollBulk(bulkJob, total);
  }

  function pollBulk(jobId, total) {
    clearInterval(bulkTimer);
    bulkTimer = setInterval(async () => {
      if (bulkCancelled) { clearInterval(bulkTimer); return; }
      let s;
      try {
        const res = await fetch("/api/bulk/status?job_id=" + encodeURIComponent(jobId));
        s = await res.json();
        if (!res.ok) throw new Error(s.error || "Lost track of the download");
      } catch (err) {
        clearInterval(bulkTimer); hideProgress(); toast(err.message, "err"); return;
      }

      // Waiting for a free download slot (server is busy with other jobs).
      if (s.status === "queued") {
        setPhase("Waiting in queue \u2014 position " + s.position +
          (s.queued_total > 1 ? " of " + s.queued_total : ""));
        setBar(0, 0);
        $("#progCurrent").textContent = "Your download starts automatically when a slot frees up\u2026";
        $("#progNote").textContent = "";
        return;
      }

      const tot = s.total || total;
      if (s.status === "running") setPhase("Downloading audio files\u2026");
      setBar(s.done, tot);
      if (s.current) $("#progCurrent").textContent = shortName(s.current);
      if (s.bytes) {
        $("#progNote").textContent = fmtBytes(s.bytes) + " downloaded" +
          (s.skipped ? " \u00b7 " + s.skipped + " skipped" : "");
      }

      if (s.status === "ready") {
        clearInterval(bulkTimer);
        setBar(tot, tot);
        setPhase("Done \u2014 starting your download");
        $("#progCancelBtn").disabled = true;
        triggerZipDownload(jobId);
        const skipped = s.skipped || 0;
        setTimeout(() => {
          hideProgress();
          toast(skipped ? ("Download ready \u00b7 " + skipped + " file(s) could not be fetched")
                        : "Download ready");
        }, 950);
        bulkJob = null;
      } else if (s.status === "error") {
        clearInterval(bulkTimer);
        hideProgress();
        toast(s.error || "The download failed", "err");
        bulkJob = null;
      }
    }, 700);
  }

  function triggerZipDownload(jobId) {
    const a = document.createElement("a");
    a.href = "/api/bulk/result?job_id=" + encodeURIComponent(jobId);
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function cancelBulk() {
    bulkCancelled = true;
    clearInterval(bulkTimer);
    if (bulkJob) {
      fetch("/api/bulk/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: bulkJob }),
      }).catch(() => {});
      bulkJob = null;
    }
    hideProgress();
    toast("Download cancelled");
  }

  // ------------------------------------------------------------ navigation
  function switchView(id) {
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === id));
  }
  function backToBrowse() { switchView("browse-view"); window.scrollTo({ top: 0 }); }
  function home() { backToBrowse(); }

  // ------------------------------------------------------------ init
  function init() {
    initFileInput();
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        if ($("#browse-view").classList.contains("active")) process();
      }
      if (e.key === "Escape") closeClip();
    });
    $("#clipModal").addEventListener("click", (e) => {
      if (e.target.id === "clipModal") closeClip();
    });
  }

  return {
    init, tab, loadSample, process, viewEpisodes, backToBrowse, home,
    toggleDesc, filterEpisodes, copyRss, viewRss, downloadRss, notifyDownload,
    openClip, closeClip, pickPreset, onRangeInput, downloadClip,
    togglePod, selectAllPods, downloadPods,
    toggleEp, selectAllEps, selectFirst, downloadEps, downloadOne, cancelBulk,
  };
})();

document.addEventListener("DOMContentLoaded", App.init);
