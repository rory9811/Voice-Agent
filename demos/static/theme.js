/* Apply the saved palette before styles load, so reloads do not flash. */
(() => {
  const system = matchMedia("(prefers-color-scheme: light)");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const valid = (value) => value === "light" || value === "dark";
  let preference;
  try {
    preference = localStorage.getItem("venus-theme");
  } catch {}

  function applyLogo(theme) {
    const light = theme === "light";
    const base = light ? "/static/venus-logo-white" : "/static/venus-logo";
    const poster = `${base}.png`;
    document.querySelectorAll("[data-brand-logo]").forEach((image) => {
      image.setAttribute("src", reducedMotion.matches ? poster : `${base}.gif`);
    });
    document.getElementById("brand-icon")?.setAttribute("href", poster);
  }

  function apply() {
    const theme = valid(preference)
      ? preference
      : system.matches ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content =
      theme === "light" ? "#f4f9fd" : "#07111f";
    applyLogo(theme);
    window.dispatchEvent(new Event("venus-theme"));
  }

  apply();
  system.addEventListener("change", apply);
  reducedMotion.addEventListener("change", () => applyLogo(document.documentElement.dataset.theme));
  window.addEventListener("storage", (event) => {
    if (event.key !== "venus-theme" && event.key !== null) return;
    preference = event.newValue;
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    applyLogo(document.documentElement.dataset.theme);
    document.getElementById("theme-button").addEventListener("click", () => {
      preference = document.documentElement.dataset.theme === "dark"
        ? "light" : "dark";
      try {
        localStorage.setItem("venus-theme", preference);
      } catch {}
      apply();
    });
  });
})();
