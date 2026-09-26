(function () {
  function formatLocal(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString(undefined, {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
      timeZoneName: "short",
    });
  }

  document.querySelectorAll("[data-utc]").forEach(function (el) {
    const raw = el.getAttribute("data-utc");
    if (!raw) return;
    el.textContent = formatLocal(raw);
    el.title = raw + " UTC";
  });
})();
