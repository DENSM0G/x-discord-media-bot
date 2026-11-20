(() => {
  "use strict";

  const payloadEl = document.getElementById("gallery-payload");
  const rootEl = document.getElementById("gallery-root");
  if (!payloadEl || !rootEl) return;

  const payload = JSON.parse(payloadEl.textContent || "{}");
  const files = Array.isArray(payload.files) ? payload.files : [];
  const statusApiUrl = payload.statusApiUrl || "";

  let currentMode = payload.defaultViewMode || "carousel";
  let lbIndex = -1;
  let preloadedOriginals = new Set();
  let resizeTimer = null;

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------

  function formatLocalTimestamps() {
    const formatter = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    document.querySelectorAll("time.local-ts[data-ts]").forEach((el) => {
      const ts = Number(el.getAttribute("data-ts"));
      if (Number.isFinite(ts) && ts > 0) {
        el.textContent = formatter.format(new Date(ts * 1000));
      }
    });
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const hrs = Math.floor(total / 3600);
    const mins = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return hrs > 0 ? `${hrs}:${pad(mins)}:${pad(secs)}` : `${mins}:${pad(secs)}`;
  }

  function splitPhotosAndVideos() {
    const visual = [];
    const videos = [];
    files.forEach((item, idx) => {
      const entry = { item, globalIndex: idx };
      if (item.type === "video") {
        videos.push(entry);
      } else {
        visual.push(entry);
      }
    });
    return { visual, videos };
  }

  function buildImageEl(item) {
    const img = document.createElement("img");
    img.src = preloadedOriginals.has(item.original_url) ? item.original_url : item.preview_url;
    img.dataset.original = item.original_url;
    img.alt = item.filename || "Image";
    img.decoding = "async";
    img.loading = "lazy";
    if (item.width && item.height) {
      img.width = item.width;
      img.height = item.height;
    }
    return img;
  }

  function buildGifVideoEl(item) {
    const vid = document.createElement("video");
    vid.src = item.original_url;
    if (item.preview_url) vid.poster = item.preview_url;
    vid.autoplay = true;
    vid.loop = true;
    vid.muted = true;
    vid.playsInline = true;
    vid.preload = "metadata";
    return vid;
  }

  function buildVisualNode(item) {
    if (item.is_mp4_gif) {
      return buildGifVideoEl(item);
    }
    return buildImageEl(item);
  }

  function buildVideoStack(videoEntries) {
    if (!videoEntries.length) return null;

    const section = document.createElement("section");
    section.className = "video-stack";

    videoEntries.forEach(({ item, globalIndex }) => {
      const card = document.createElement("div");
      card.className = "video-card";

      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      if (item.preview_url) video.poster = item.preview_url;
      video.src = item.original_url;
      card.appendChild(video);

      const bar = document.createElement("div");
      bar.className = "video-card-bar";

      const info = document.createElement("span");
      const durText = item.duration ? ` · ${formatDuration(item.duration)}` : "";
      info.textContent = `${item.filename} (${item.size_human || ""}${durText})`;
      bar.appendChild(info);

      const actions = document.createElement("div");
      actions.style.display = "flex";
      actions.style.gap = "8px";

      const fullBtn = document.createElement("button");
      fullBtn.type = "button";
      fullBtn.className = "btn btn-ghost btn-sm";
      fullBtn.textContent = "⛶ Lightbox";
      fullBtn.addEventListener("click", () => {
        video.pause();
        openLightbox(globalIndex);
      });
      actions.appendChild(fullBtn);

      const dlLink = document.createElement("a");
      dlLink.className = "btn btn-secondary btn-sm";
      dlLink.href = item.original_url;
      dlLink.download = item.filename;
      dlLink.textContent = "⬇ Download";
      actions.appendChild(dlLink);

      bar.appendChild(actions);
      card.appendChild(bar);
      section.appendChild(card);
    });

    return section;
  }

  // ------------------------------------------------------------------
  // Background original preloader (fast WebP first, originals in background)
  // ------------------------------------------------------------------

  function preloadOriginalsInBackground() {
    const queue = files.filter(
      (f) => f.type !== "video" && !f.is_mp4_gif && f.original_url && f.original_url !== f.preview_url
    );
    if (!queue.length) return;

    let cursor = 0;
    const concurrency = 2;

    function next() {
      if (cursor >= queue.length) return;
      const item = queue[cursor++];
      const loader = new Image();
      loader.decoding = "async";
      loader.onload = () => {
        preloadedOriginals.add(item.original_url);
        document.querySelectorAll(`img[data-original="${CSS.escape(item.original_url)}"]`).forEach((img) => {
          if (img.src !== item.original_url) {
            img.src = item.original_url;
          }
        });
        next();
      };
      loader.onerror = () => next();
      loader.src = item.original_url;
    }

    const start = () => {
      for (let i = 0; i < Math.min(concurrency, queue.length); i++) {
        next();
      }
    };

    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(start, { timeout: 1500 });
    } else {
      setTimeout(start, 300);
    }
  }

  // ------------------------------------------------------------------
  // Mode 1: Horizontal Carousel (no gaps, wheel/swipe/scrollbar/buttons + videos below)
  // ------------------------------------------------------------------

  function renderCarousel() {
    const { visual, videos } = splitPhotosAndVideos();

    if (visual.length) {
      const shell = document.createElement("section");
      shell.className = "carousel-shell";

      const track = document.createElement("div");
      track.className = "carousel-track";

      visual.forEach(({ item, globalIndex }) => {
        const slide = document.createElement("figure");
        slide.className = "carousel-item media-clickable";
        slide.appendChild(buildVisualNode(item));

        if (item.type === "gif") {
          const badge = document.createElement("span");
          badge.className = "media-badge";
          badge.textContent = "GIF";
          slide.appendChild(badge);
        }

        slide.addEventListener("click", () => openLightbox(globalIndex));
        track.appendChild(slide);
      });

      // Vertical mouse wheel scrolls carousel horizontally when hovered
      track.addEventListener(
        "wheel",
        (ev) => {
          if (track.scrollWidth <= track.clientWidth) return;
          if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
            const atLeftEdge = track.scrollLeft <= 1 && ev.deltaY < 0;
            const atRightEdge = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1 && ev.deltaY > 0;
            if (!atLeftEdge && !atRightEdge) {
              ev.preventDefault();
              track.scrollLeft += ev.deltaY;
            }
          }
        },
        { passive: false }
      );

      shell.appendChild(track);

      // Prev / Next buttons below the carousel
      const controls = document.createElement("div");
      controls.className = "carousel-controls";

      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.className = "btn btn-secondary btn-sm";
      prevBtn.textContent = "← Prev";
      prevBtn.addEventListener("click", () => {
        const step = Math.max(280, Math.round(track.clientWidth * 0.72));
        track.scrollBy({ left: -step, behavior: "smooth" });
      });

      const hint = document.createElement("span");
      hint.className = "carousel-hint";
      hint.textContent = `${visual.length} image${visual.length === 1 ? "" : "s"} · scroll, swipe or click to zoom`;

      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "btn btn-secondary btn-sm";
      nextBtn.textContent = "Next →";
      nextBtn.addEventListener("click", () => {
        const step = Math.max(280, Math.round(track.clientWidth * 0.72));
        track.scrollBy({ left: step, behavior: "smooth" });
      });

      controls.append(prevBtn, hint, nextBtn);
      shell.appendChild(controls);
      rootEl.appendChild(shell);
    }

    const videoSection = buildVideoStack(videos);
    if (videoSection) {
      if (visual.length) {
        const label = document.createElement("div");
        label.className = "section-label";
        label.textContent = `Videos (${videos.length})`;
        rootEl.appendChild(label);
      }
      rootEl.appendChild(videoSection);
    }
  }

  // ------------------------------------------------------------------
  // Mode 2: Comic (strict original post order top-to-bottom)
  // ------------------------------------------------------------------

  function renderComic() {
    const stack = document.createElement("section");
    stack.className = "comic-stack";

    files.forEach((item, globalIndex) => {
      const box = document.createElement("div");
      box.className = "comic-item";

      if (item.type === "video") {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        if (item.preview_url) video.poster = item.preview_url;
        video.src = item.original_url;
        box.appendChild(video);
      } else {
        box.classList.add("media-clickable");
        box.appendChild(buildVisualNode(item));
        if (item.type === "gif") {
          const badge = document.createElement("span");
          badge.className = "media-badge";
          badge.textContent = "GIF";
          box.appendChild(badge);
        }
        box.addEventListener("click", () => openLightbox(globalIndex));
      }

      stack.appendChild(box);
    });

    rootEl.appendChild(stack);
  }

  // ------------------------------------------------------------------
  // Mode 3: Smart Tiles (Balanced Masonry preserving original aspect ratios + videos below)
  // ------------------------------------------------------------------

  function computeMasonryColumns(count) {
    const w = rootEl.clientWidth || window.innerWidth;
    let cols = 1;
    if (w >= 1200) cols = 4;
    else if (w >= 820) cols = 3;
    else if (w >= 480) cols = 2;
    return Math.max(1, Math.min(cols, count));
  }

  function renderMasonry() {
    const { visual, videos } = splitPhotosAndVideos();

    if (visual.length) {
      const colCount = computeMasonryColumns(visual.length);
      const container = document.createElement("section");
      container.className = "masonry-columns";

      const cols = [];
      const colHeights = new Array(colCount).fill(0);

      for (let i = 0; i < colCount; i++) {
        const col = document.createElement("div");
        col.className = "masonry-col";
        cols.push(col);
        container.appendChild(col);
      }

      visual.forEach(({ item, globalIndex }) => {
        // Place next tile into the currently shortest column (preserves left-to-right flow)
        let minIdx = 0;
        for (let i = 1; i < colCount; i++) {
          if (colHeights[i] < colHeights[minIdx]) minIdx = i;
        }

        const ratio = item.width && item.height ? item.height / item.width : 1.0;
        colHeights[minIdx] += ratio;

        const tile = document.createElement("div");
        tile.className = "masonry-item media-clickable";
        tile.appendChild(buildVisualNode(item));

        if (item.type === "gif") {
          const badge = document.createElement("span");
          badge.className = "media-badge";
          badge.textContent = "GIF";
          tile.appendChild(badge);
        }

        tile.addEventListener("click", () => openLightbox(globalIndex));
        cols[minIdx].appendChild(tile);
      });

      rootEl.appendChild(container);
    }

    const videoSection = buildVideoStack(videos);
    if (videoSection) {
      if (visual.length) {
        const label = document.createElement("div");
        label.className = "section-label";
        label.textContent = `Videos (${videos.length})`;
        rootEl.appendChild(label);
      }
      rootEl.appendChild(videoSection);
    }
  }

  // ------------------------------------------------------------------
  // Mode 4: Square Grid (contain fit, dynamic cols from aspect ratio & DPR, videos open only fullscreen)
  // ------------------------------------------------------------------

  function computeGridColumns(totalItems) {
    const vw = Math.max(320, window.innerWidth);
    const vh = Math.max(320, window.innerHeight);
    const aspect = vw / vh;
    const dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1));

    // Higher pixel density displays remain readable with slightly more compact tiles
    const densityFactor = 1 + (dpr - 1) * 0.14;
    const baseTilePx = vw < 640 ? 155 : 230;
    const widthCols = (vw / baseTilePx) * densityFactor;
    const aspectBias = aspect >= 1.35 ? 1.1 : aspect <= 0.8 ? 0.88 : 1.0;

    let cols = Math.round(widthCols * aspectBias);
    cols = Math.max(2, Math.min(8, cols));

    // Avoid overly tiny tiles when post only has 1–2 items
    if (totalItems > 0 && totalItems < cols) {
      cols = Math.max(1, Math.min(cols, totalItems));
    }
    return cols;
  }

  function renderGrid() {
    const grid = document.createElement("section");
    grid.className = "square-grid";
    grid.style.setProperty("--grid-cols", String(computeGridColumns(files.length)));

    files.forEach((item, globalIndex) => {
      const tile = document.createElement("div");
      tile.className = "grid-tile media-clickable";

      if (item.type === "video") {
        // Static poster only; playback happens exclusively in full-screen Lightbox
        const poster = document.createElement("img");
        poster.src = item.preview_url || "";
        poster.alt = item.filename || "Video";
        poster.loading = "lazy";
        poster.decoding = "async";
        tile.appendChild(poster);

        const overlay = document.createElement("div");
        overlay.className = "play-overlay";
        const circle = document.createElement("div");
        circle.className = "play-circle";
        circle.textContent = "▶";
        overlay.appendChild(circle);
        tile.appendChild(overlay);

        if (item.duration) {
          const dur = document.createElement("span");
          dur.className = "duration-pill";
          dur.textContent = formatDuration(item.duration);
          tile.appendChild(dur);
        }
      } else {
        tile.appendChild(buildVisualNode(item));
        if (item.type === "gif") {
          const badge = document.createElement("span");
          badge.className = "media-badge";
          badge.textContent = "GIF";
          tile.appendChild(badge);
        }
      }

      tile.addEventListener("click", () => openLightbox(globalIndex));
      grid.appendChild(tile);
    });

    rootEl.appendChild(grid);
  }

  // ------------------------------------------------------------------
  // Mode Switching
  // ------------------------------------------------------------------

  function renderCurrentMode() {
    rootEl.innerHTML = "";

    document.querySelectorAll(".mode-btn[data-mode]").forEach((btn) => {
      const active = btn.dataset.mode === currentMode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });

    if (!files.length) {
      const empty = document.createElement("p");
      empty.style.color = "var(--text-muted)";
      empty.textContent = "No media files found for this post.";
      rootEl.appendChild(empty);
      return;
    }

    switch (currentMode) {
      case "comic":
        renderComic();
        break;
      case "masonry":
        renderMasonry();
        break;
      case "grid":
        renderGrid();
        break;
      case "carousel":
      default:
        currentMode = "carousel";
        renderCarousel();
        break;
    }
  }

  document.querySelectorAll(".mode-btn[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetMode = btn.dataset.mode;
      if (targetMode && targetMode !== currentMode) {
        currentMode = targetMode;
        renderCurrentMode();
      }
    });
  });

  window.addEventListener("resize", () => {
    if (currentMode !== "grid" && currentMode !== "masonry") return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (currentMode === "grid") {
        const gridEl = rootEl.querySelector(".square-grid");
        if (gridEl) {
          gridEl.style.setProperty("--grid-cols", String(computeGridColumns(files.length)));
        }
      } else if (currentMode === "masonry") {
        renderCurrentMode();
      }
    }, 140);
  });

  // ------------------------------------------------------------------
  // Lightbox (Full-screen viewer for images, GIFs, and videos)
  // ------------------------------------------------------------------

  const lbEl = document.getElementById("lightbox");
  const lbStage = document.getElementById("lb-stage");
  const lbCounter = document.getElementById("lb-counter");
  const lbMeta = document.getElementById("lb-meta");
  const lbDownload = document.getElementById("lb-download");
  const lbPrev = document.getElementById("lb-prev");
  const lbNext = document.getElementById("lb-next");
  const lbClose = document.getElementById("lb-close");

  function stopStageVideo() {
    const vid = lbStage.querySelector("video");
    if (vid) {
      vid.pause();
      vid.removeAttribute("src");
      vid.load();
    }
  }

  function renderLightboxItem() {
    if (lbIndex < 0 || lbIndex >= files.length) return;
    stopStageVideo();
    lbStage.innerHTML = "";

    const item = files[lbIndex];
    lbCounter.textContent = `${lbIndex + 1} / ${files.length}`;

    const dims = item.width && item.height ? `${item.width}×${item.height}` : "";
    const parts = [item.filename, dims, item.size_human].filter(Boolean);
    lbMeta.textContent = parts.join(" · ");

    lbDownload.href = item.original_url;
    lbDownload.setAttribute("download", item.filename);

    const showNav = files.length > 1;
    lbPrev.style.display = showNav ? "" : "none";
    lbNext.style.display = showNav ? "" : "none";

    if (item.type === "video") {
      const video = document.createElement("video");
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      if (item.preview_url) video.poster = item.preview_url;
      video.src = item.original_url;
      lbStage.appendChild(video);
    } else if (item.is_mp4_gif) {
      const gifVid = buildGifVideoEl(item);
      gifVid.controls = true;
      lbStage.appendChild(gifVid);
    } else {
      const img = document.createElement("img");
      img.src = item.original_url;
      img.alt = item.filename || "Original";
      img.addEventListener("click", (ev) => {
        ev.stopPropagation();
        img.classList.toggle("zoomed");
      });
      lbStage.appendChild(img);
    }
  }

  function openLightbox(index) {
    if (!files.length) return;
    lbIndex = ((index % files.length) + files.length) % files.length;
    lbEl.hidden = false;
    document.body.style.overflow = "hidden";
    renderLightboxItem();
  }

  function closeLightbox() {
    if (lbEl.hidden) return;
    stopStageVideo();
    lbStage.innerHTML = "";
    lbEl.hidden = true;
    lbIndex = -1;
    document.body.style.overflow = "";
  }

  function stepLightbox(delta) {
    if (lbEl.hidden || files.length <= 1) return;
    lbIndex = (lbIndex + delta + files.length) % files.length;
    renderLightboxItem();
  }

  lbClose.addEventListener("click", closeLightbox);
  lbPrev.addEventListener("click", () => stepLightbox(-1));
  lbNext.addEventListener("click", () => stepLightbox(1));

  lbStage.addEventListener("click", (ev) => {
    if (ev.target === lbStage) closeLightbox();
  });

  document.addEventListener("keydown", (ev) => {
    if (lbEl.hidden) return;
    if (ev.key === "Escape") closeLightbox();
    else if (ev.key === "ArrowLeft") stepLightbox(-1);
    else if (ev.key === "ArrowRight") stepLightbox(1);
  });

  // Touch swipe inside Lightbox
  let touchStartX = 0;
  let touchStartY = 0;
  lbStage.addEventListener(
    "touchstart",
    (ev) => {
      if (ev.touches.length !== 1) return;
      touchStartX = ev.touches[0].clientX;
      touchStartY = ev.touches[0].clientY;
    },
    { passive: true }
  );
  lbStage.addEventListener(
    "touchend",
    (ev) => {
      if (ev.changedTouches.length !== 1) return;
      const dx = ev.changedTouches[0].clientX - touchStartX;
      const dy = ev.changedTouches[0].clientY - touchStartY;
      if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.4) {
        stepLightbox(dx < 0 ? 1 : -1);
      }
    },
    { passive: true }
  );

  // ------------------------------------------------------------------
  // Live Footer Storage & FIFO Queue Updater
  // ------------------------------------------------------------------

  const diskFreeEl = document.getElementById("st-disk-free");
  const diskHeadroomEl = document.getElementById("st-disk-headroom");
  const queuePosEl = document.getElementById("st-queue-pos");
  const queueDetailEl = document.getElementById("st-queue-detail");
  const refreshBtn = document.getElementById("st-refresh-btn");

  async function refreshStorageStatus() {
    if (!statusApiUrl) return;
    if (refreshBtn) refreshBtn.disabled = true;

    try {
      const resp = await fetch(statusApiUrl, { cache: "no-store" });
      if (resp.status === 410) {
        if (queuePosEl) {
          queuePosEl.textContent = "Removed from bot server";
          queuePosEl.classList.add("storage-removed");
        }
        if (queueDetailEl) {
          queueDetailEl.textContent = "This post is no longer stored on disk.";
        }
        return;
      }
      if (!resp.ok) return;

      const data = await resp.json();
      if (diskFreeEl) {
        diskFreeEl.textContent = `${data.disk_free_human} free / ${data.disk_total_human}`;
      }
      if (diskHeadroomEl) {
        diskHeadroomEl.textContent = `Cleanup reserve: ${data.headroom_human} (threshold ${data.min_free_human})`;
      }
      if (queuePosEl) {
        queuePosEl.textContent = `#${data.position} of ${data.total_posts}`;
      }
      if (queueDetailEl) {
        if (data.older_posts_count === 0) {
          queueDetailEl.textContent = `Next in line when disk fills up (this post: ${data.current_post_human})`;
        } else {
          const plural = data.older_posts_count === 1 ? "" : "s";
          queueDetailEl.textContent = `${data.older_posts_count} older post${plural} (${data.older_posts_human}) ahead · this post: ${data.current_post_human}`;
        }
      }
    } catch (_) {
      // Silently ignore transient network hiccups
    } finally {
      if (refreshBtn) refreshBtn.disabled = false;
    }
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshStorageStatus);
  }
  setInterval(() => {
    if (document.visibilityState === "visible") {
      refreshStorageStatus();
    }
  }, 60000);

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  formatLocalTimestamps();
  renderCurrentMode();
  preloadOriginalsInBackground();
})();