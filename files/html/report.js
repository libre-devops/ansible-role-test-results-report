(() => {
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const quote = (value) => /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  // The text a cell sorts, filters and copies by: its data-value when it has one (a severity,
  // a number), else what it shows, without the expandable details.
  const cellText = (cell) => {
    if (cell.dataset.value !== undefined) return cell.dataset.value;
    const copy = cell.cloneNode(true);
    copy.querySelectorAll("details").forEach((node) => node.remove());
    return copy.textContent.trim();
  };
  for (const panel of document.querySelectorAll(".panel")) {
    const filter = panel.querySelector(".toolbar input");
    const table = panel.querySelector("table");
    if (!filter || !table) continue;
    const body = table.tBodies[0];
    const rows = [...body.rows];
    const count = panel.querySelector(".count");
    const copy = panel.querySelector(".toolbar button");
    const heads = [...table.tHead.rows[0].cells];
    const shown = () => rows.filter((row) => !row.hidden);
    const counted = () => { count.textContent = `${shown().length} of ${rows.length} rows`; };
    filter.addEventListener("input", () => {
      const wanted = filter.value.trim().toLowerCase();
      for (const row of rows) {
        row.hidden = wanted !== "" && !row.textContent.toLowerCase().includes(wanted);
      }
      counted();
    });
    heads.forEach((head, index) => head.addEventListener("click", () => {
      const order = head.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
      heads.forEach((other) => other.removeAttribute("aria-sort"));
      head.setAttribute("aria-sort", order);
      const sign = order === "ascending" ? 1 : -1;
      rows.sort((a, b) => sign * collator.compare(cellText(a.cells[index]), cellText(b.cells[index])));
      body.append(...rows);
    }));
    copy.addEventListener("click", () => {
      const lines = [heads.map((head) => quote(head.textContent))];
      for (const row of shown()) lines.push([...row.cells].map((cell) => quote(cellText(cell))));
      const text = lines.map((line) => line.join(",")).join("\n");
      const done = () => {
        copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy as CSV"; }, 1500);
      };
      if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done); return; }
      const area = document.createElement("textarea");
      area.value = text;
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      done();
    });
    counted();
  }
})();
