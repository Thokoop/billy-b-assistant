// ===================== SEARCHABLE SELECT =====================
// A text input with a filtered dropdown, styled like the rest of the Web UI.
//
// This replaces <input list="..."> + <datalist>. The native datalist popup is
// drawn by the browser: its width, position and styling cannot be reached from
// CSS, so on a long list it renders narrower than the field it belongs to and
// in the browser's own colours.
//
// Use it on any text input:
//
//     <input id="timezone" data-searchable-select ...>
//     SearchableSelect.setOptions("timezone", ["Europe/Amsterdam", ...]);
//
// Options are strings, or {value, label} for a different display text. The
// input keeps its own id, name and value, so form handling is unchanged: the
// component only dispatches "input" and "change" on it when a option is picked.
const SearchableSelect = (() => {
    const instances = new WeakMap();

// The panel's z-index only orders it against its siblings inside the nearest
// ancestor that creates a stacking context. If a card wrapping this field
// creates one - the settings cards do, via backdrop-filter - then the next
// card paints over the panel however high its z-index is. Lifting that
// ancestor while the panel is open is what actually puts the panel on top.
const stackingContextAncestor = (element) => {
    for (
        let node = element.parentElement;
        node && node !== document.body;
        node = node.parentElement
    ) {
        const style = getComputedStyle(node);
        const backdrop = style.backdropFilter || style.webkitBackdropFilter;
        if (
            (style.position !== "static" && style.zIndex !== "auto")
            || style.opacity !== "1"
            || style.transform !== "none"
            || style.filter !== "none"
            || (backdrop && backdrop !== "none")
            || style.isolation === "isolate"
            || (style.mixBlendMode && style.mixBlendMode !== "normal")
            || /transform|opacity|filter/.test(style.willChange || "")
            || /paint|layout|content|strict/.test(style.contain || "")
        ) {
            return node;
        }
    }
    return null;
};


    const PANEL_CLASSES = [
        "searchable-select-panel", "absolute", "left-0", "right-0", "z-50",
        "mt-1", "max-h-64", "overflow-y-auto", "rounded", "bg-zinc-800",
        "border", "border-zinc-700", "shadow-lg", "hidden",
    ];
    const OPTION_CLASSES = [
        "px-3", "py-2", "text-white", "cursor-pointer", "truncate",
    ];

    const normalize = (option) => {
        if (option === null || option === undefined) return null;
        if (typeof option === "string") return {value: option, label: option};
        const value = String(option.value ?? "");
        return {value, label: String(option.label ?? value)};
    };

    const resolve = (target) =>
        typeof target === "string" ? document.getElementById(target) : target;

    class Instance {
        constructor(input) {
            this.input = input;
            this.options = [];
            this.matches = [];
            this.activeIndex = -1;
            this.open = false;
            this.selecting = false;
            this.lifted = null;
            this.liftedStyle = null;

            // The panel is sized off a wrapper around the input alone, so it is
            // exactly as wide as the field however the field is laid out.
            this.wrapper = document.createElement("div");
            this.wrapper.className = "relative w-full min-w-0";
            input.parentNode.insertBefore(this.wrapper, input);
            this.wrapper.appendChild(input);

            this.panel = document.createElement("div");
            this.panel.className = PANEL_CLASSES.join(" ");
            this.panel.setAttribute("role", "listbox");
            this.wrapper.appendChild(this.panel);

            // The native popup would open alongside this one.
            input.removeAttribute("list");
            input.setAttribute("autocomplete", "off");
            input.setAttribute("role", "combobox");
            input.setAttribute("aria-expanded", "false");
            input.setAttribute("aria-autocomplete", "list");

            input.addEventListener("focus", () => this.show());
            input.addEventListener("click", () => this.show());
            input.addEventListener("input", () => {
                // choose() dispatches "input" so form code sees the change; that
                // must not reopen the panel it just closed.
                if (this.selecting) return;
                this.show();
            });
            input.addEventListener("keydown", (event) => this.onKeyDown(event));
            input.addEventListener("blur", () => {
                // Let a click on an option land before the panel goes away.
                setTimeout(() => this.hide(), 120);
            });
            this.panel.addEventListener("mousedown", (event) => {
                // Keep focus on the input so blur does not fire mid-click.
                event.preventDefault();
            });
        }

        setOptions(options) {
            this.options = (options || []).map(normalize).filter(Boolean);
            if (this.open) this.render();
        }

        filtered() {
            const query = this.input.value.trim().toLowerCase();
            if (!query) return this.options;
            const starts = [];
            const contains = [];
            for (const option of this.options) {
                const haystack = option.label.toLowerCase();
                const at = haystack.indexOf(query);
                if (at === 0) starts.push(option);
                else if (at > 0) contains.push(option);
            }
            return starts.concat(contains);
        }

        render() {
            this.matches = this.filtered();
            if (!this.matches.length) {
                this.panel.replaceChildren();
                this.hide();
                return;
            }
            const current = this.input.value.trim();
            this.activeIndex = Math.max(
                0, this.matches.findIndex((option) => option.value === current)
            );
            this.panel.replaceChildren(...this.matches.map((option, index) => {
                const row = document.createElement("div");
                row.className = OPTION_CLASSES.join(" ");
                row.textContent = option.label;
                row.title = option.label;
                row.setAttribute("role", "option");
                row.addEventListener("mouseenter", () => this.setActive(index));
                row.addEventListener("click", () => this.choose(index));
                return row;
            }));
            this.paintActive(true);
        }

        setActive(index) {
            if (index < 0 || index >= this.matches.length) return;
            this.activeIndex = index;
            this.paintActive(false);
        }

        paintActive(scroll) {
            const rows = Array.from(this.panel.children);
            rows.forEach((row, index) => {
                const active = index === this.activeIndex;
                row.classList.toggle("bg-zinc-700", active);
                // text-white and text-cyan-400 have equal specificity, so the
                // white has to come off or Tailwind's source order wins.
                row.classList.toggle("text-white", !active);
                row.classList.toggle("text-cyan-400", active);
                row.setAttribute("aria-selected", active ? "true" : "false");
            });
            const row = rows[this.activeIndex];
            if (row && scroll) row.scrollIntoView({block: "nearest"});
            else if (row) {
                const top = row.offsetTop;
                const bottom = top + row.offsetHeight;
                if (top < this.panel.scrollTop) this.panel.scrollTop = top;
                else if (bottom > this.panel.scrollTop + this.panel.clientHeight) {
                    this.panel.scrollTop = bottom - this.panel.clientHeight;
                }
            }
        }

        choose(index) {
            const option = this.matches[index];
            if (!option) return;
            this.input.value = option.value;
            this.hide();
            this.selecting = true;
            try {
                this.input.dispatchEvent(new Event("input", {bubbles: true}));
                this.input.dispatchEvent(new Event("change", {bubbles: true}));
            } finally {
                this.selecting = false;
            }
        }

        show() {
            if (!this.options.length) return;
            this.render();
            if (!this.matches.length) return;
            this.panel.classList.remove("hidden");
            this.input.setAttribute("aria-expanded", "true");
            this.open = true;
            this.lift();
            this.placeholderFlip();
        }

        // A long list near the bottom of the page opens upward instead.
        placeholderFlip() {
            const room = window.innerHeight - this.input.getBoundingClientRect().bottom;
            const above = room < Math.min(this.panel.scrollHeight + 16, 272);
            this.panel.classList.toggle("bottom-full", above);
            this.panel.classList.toggle("mb-1", above);
            this.panel.classList.toggle("mt-1", !above);
        }

        lift() {
            this.drop();
            const ancestor = stackingContextAncestor(this.input);
            if (!ancestor) return;
            this.lifted = ancestor;
            this.liftedStyle = {
                position: ancestor.style.position,
                zIndex: ancestor.style.zIndex,
            };
            if (getComputedStyle(ancestor).position === "static") {
                ancestor.style.position = "relative";
            }
            ancestor.style.zIndex = "40";
        }

        // Always restore what was there: these cards are laid out in CSS
        // columns, and a leftover z-index would quietly reorder them.
        drop() {
            if (!this.lifted) return;
            this.lifted.style.position = this.liftedStyle.position;
            this.lifted.style.zIndex = this.liftedStyle.zIndex;
            this.lifted = null;
            this.liftedStyle = null;
        }

        hide() {
            this.panel.classList.add("hidden");
            this.input.setAttribute("aria-expanded", "false");
            this.open = false;
            this.drop();
        }

        onKeyDown(event) {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (!this.open) {
                    this.show();
                    return;
                }
                const step = event.key === "ArrowDown" ? 1 : -1;
                const next = this.activeIndex + step;
                if (next >= 0 && next < this.matches.length) this.setActive(next);
                return;
            }
            if (event.key === "Enter" && this.open) {
                event.preventDefault();
                this.choose(this.activeIndex);
                return;
            }
            if (event.key === "Escape" && this.open) {
                event.preventDefault();
                this.hide();
            }
        }
    }

    const attach = (target) => {
        const input = resolve(target);
        if (!input) return null;
        let instance = instances.get(input);
        if (!instance) {
            instance = new Instance(input);
            instances.set(input, instance);
        }
        return instance;
    };

    const setOptions = (target, options) => {
        const instance = attach(target);
        if (instance) instance.setOptions(options);
        return instance;
    };

    // Upgrades every [data-searchable-select] input under root.
    const initAll = (root = document) => {
        root.querySelectorAll("[data-searchable-select]").forEach(attach);
    };

    return {attach, setOptions, initAll};
})();

window.SearchableSelect = SearchableSelect;
