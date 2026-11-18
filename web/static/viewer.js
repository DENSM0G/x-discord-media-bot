(() => {
  const dataEl = document.getElementById("visuals-data");
  if (!dataEl) return;

  let visuals = [];
  try {
    visuals = JSON.parse(dataEl.textContent || "[]");
  } catch {
    return;
  }
  if (!visuals.length) return;

  // 1. Фоновая последовательная предзагрузка оригиналов после загрузки WebP-превью
  function preloadOriginals() {
    const images = visuals.filter((item) => item.type === "image");
    let idx = 0;

    function loadNext() {
      if (idx >= images.length) return;
      const img = new Image();
      img.onload = img.onerror = () => {
        idx += 1;
        loadNext();
      };
      img.src = images[idx].original;
    }

    loadNext();
  }

  window.addEventListener("load", preloadOriginals);

  // 2. Управление каруселью
  const stage = document.getElementById("visual-stage");
  const items = Array.from(document.querySelectorAll(".visual-item"));
  const thumbs = Array.from(document.querySelectorAll(".thumb-btn"));
  const counterEl = document.getElementById("carousel-counter");
  const prevBtn = document.getElementById("carousel-prev");
  const nextBtn = document.getElementById("carousel-next");
  const toggleLayoutBtn = document.getElementById("toggle-layout-btn");
  const thumbStrip = document.getElementById("thumb-strip");

  let currentIndex = 0;

  function showSlide(index) {
    if (!items.length) return;
    currentIndex = (index + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle("active", i === currentIndex));
    thumbs.forEach((el, i) => el.classList.toggle("active", i === currentIndex));
    if (counterEl) {
      counterEl.textContent = `${currentIndex + 1} / ${items.length}`;
    }
    if (thumbs[currentIndex]) {
      thumbs[currentIndex].scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
    }
  }

  if (prevBtn) prevBtn.addEventListener("click", () => showSlide(currentIndex - 1));
  if (nextBtn) nextBtn.addEventListener("click", () => showSlide(currentIndex + 1));
  thumbs.forEach((btn) => {
    btn.addEventListener("click", () => showSlide(Number(btn.dataset.index || 0)));
  });

  if (toggleLayoutBtn && stage) {
    toggleLayoutBtn.addEventListener("click", () => {
      const isCarousel = stage.classList.contains("layout-carousel");
      if (isCarousel) {
        stage.classList.remove("layout-carousel");
        stage.classList.add("layout-grid");
        if (prevBtn) prevBtn.hidden = true;
        if (nextBtn) nextBtn.hidden = true;
        if (thumbStrip) thumbStrip.hidden = true;
        toggleLayoutBtn.textContent = "Вернуть карусель";
      } else {
        stage.classList.remove("layout-grid");
        stage.classList.add("layout-carousel");
        if (prevBtn) prevBtn.hidden = false;
        if (nextBtn) nextBtn.hidden = false;
        if (thumbStrip) thumbStrip.hidden = false;
        toggleLayoutBtn.textContent = "Показать сеткой";
        showSlide(currentIndex);
      }
    });
  }

  // Свайпы на сенсорных экранах в режиме карусели
  if (stage) {
    let touchStartX = 0;
    stage.addEventListener("touchstart", (e) => {
      touchStartX = e.changedTouches[0].screenX;
    }, { passive: true });

    stage.addEventListener("touchend", (e) => {
      if (!stage.classList.contains("layout-carousel")) return;
      const diff = e.changedTouches[0].screenX - touchStartX;
      if (Math.abs(diff) > 45) {
        showSlide(diff < 0 ? currentIndex + 1 : currentIndex - 1);
      }
    }, { passive: true });
  }

  // 3. Полноэкранный Lightbox для просмотра оригиналов
  const lightbox = document.getElementById("lightbox");
  const lbImg = document.getElementById("lb-img");
  const lbClose = document.getElementById("lb-close");
  const lbPrev = document.getElementById("lb-prev");
  const lbNext = document.getElementById("lb-next");
  const lbCaption = document.getElementById("lb-caption");

  const imageItems = visuals.filter((v) => v.type === "image");
  let lbPos = 0;

  function openLightbox(visualIndex) {
    const pos = imageItems.findIndex((v) => v.index === visualIndex);
    if (pos === -1 || !lightbox || !lbImg) return;
    lbPos = pos;
    renderLightbox();
    lightbox.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function renderLightbox() {
    const item = imageItems[lbPos];
    if (!item || !lbImg) return;
    lbImg.classList.remove("zoomed");
    lbImg.src = item.original;
    if (lbCaption) {
      lbCaption.textContent = `${item.filename} • ${item.width}×${item.height} • ${item.size}`;
    }
    if (lbPrev) lbPrev.hidden = imageItems.length <= 1;
    if (lbNext) lbNext.hidden = imageItems.length <= 1;
  }

  function closeLightbox() {
    if (!lightbox) return;
    lightbox.hidden = true;
    document.body.style.overflow = "";
  }

  document.querySelectorAll(".zoomable-img").forEach((imgEl) => {
    imgEl.addEventListener("click", () => {
      openLightbox(Number(imgEl.dataset.index || 0));
    });
  });

  if (lbImg) {
    lbImg.addEventListener("click", () => lbImg.classList.toggle("zoomed"));
  }
  if (lbClose) lbClose.addEventListener("click", closeLightbox);
  if (lbPrev) {
    lbPrev.addEventListener("click", () => {
      lbPos = (lbPos - 1 + imageItems.length) % imageItems.length;
      renderLightbox();
    });
  }
  if (lbNext) {
    lbNext.addEventListener("click", () => {
      lbPos = (lbPos + 1) % imageItems.length;
      renderLightbox();
    });
  }
  if (lightbox) {
    lightbox.addEventListener("click", (e) => {
      if (e.target === lightbox) closeLightbox();
    });
  }

  document.addEventListener("keydown", (e) => {
    if (lightbox && !lightbox.hidden) {
      if (e.key === "Escape") closeLightbox();
      if (e.key === "ArrowLeft" && lbPrev) lbPrev.click();
      if (e.key === "ArrowRight" && lbNext) lbNext.click();
      return;
    }
    if (stage && stage.classList.contains("layout-carousel")) {
      if (e.key === "ArrowLeft") showSlide(currentIndex - 1);
      if (e.key === "ArrowRight") showSlide(currentIndex + 1);
    }
  });
})();