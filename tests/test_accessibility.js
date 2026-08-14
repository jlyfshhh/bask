// Browser-free behavior checks for Bask's modal focus management. These use a
// deliberately tiny DOM rather than a browser automation dependency, so they
// stay fast enough to run in every CI job.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "frontend", "app.js"), "utf8");
const code = source.slice(0, source.indexOf("// ── clock"));

let documentMock;
class ClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}
class HTMLElementMock {
  constructor(id, tagName = "DIV") {
    this.id = id;
    this.tagName = tagName;
    this.classList = new ClassList();
    this.attributes = new Map();
    this.controls = [];
    this.inert = false;
    this.isConnected = true;
    this.visible = true;
    this.parentDialog = null;
    this.focusCount = 0;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  querySelectorAll() { return this.controls; }
  querySelector(selector) { return this.controls.find(node => node.selector === selector) || null; }
  getClientRects() { return this.visible ? [{}] : []; }
  closest(selector) {
    if (selector !== "[inert]") return null;
    return this.inert ? this : (this.parentDialog?.inert ? this.parentDialog : null);
  }
  focus() { this.focusCount += 1; documentMock.activeElement = this; }
}

function dialog(id, controlNames) {
  const node = new HTMLElementMock(id);
  node.inert = true;
  node.setAttribute("aria-hidden", "true");
  node.controls = controlNames.map(name => {
    const control = new HTMLElementMock(name, "BUTTON");
    control.parentDialog = node;
    return control;
  });
  return node;
}

const external = new HTMLElementMock("manage-opener", "BUTTON");
const header = new HTMLElementMock("header", "HEADER");
const toast = new HTMLElementMock("toast");
const manage = dialog("manage", ["manage-first", "manage-last"]);
const editor = dialog("editor", ["editor-first", "editor-last"]);
const script = new HTMLElementMock("script", "SCRIPT");
const elements = { manage, editor };
let keydown;
documentMock = {
  activeElement: external,
  body: { children: [header, toast, manage, editor, script] },
  getElementById: id => elements[id] || null,
  addEventListener: (name, handler) => { if (name === "keydown") keydown = handler; },
};

const context = vm.createContext({
  console,
  document: documentMock,
  HTMLElement: HTMLElementMock,
  requestAnimationFrame: callback => callback(),
  fetch: async () => { throw new Error("not used"); },
  MutationObserver: class {},
});
vm.runInContext(code, context, { filename: "frontend/app.js" });

vm.runInContext('openDialog("manage")', context);
assert.equal(manage.inert, false);
assert.equal(manage.getAttribute("aria-hidden"), "false");
assert.equal(manage.classList.contains("open"), true);
assert.equal(documentMock.activeElement, manage.controls[0], "entry focus must use the first visible control");
assert.equal(header.inert, true, "dashboard background must become inert");
assert.equal(header.getAttribute("aria-hidden"), "true");
assert.equal(toast.inert, false, "live toast must remain available to assistive technology");

documentMock.activeElement = manage.controls[1];
let prevented = false;
keydown({ key: "Tab", shiftKey: false, preventDefault() { prevented = true; } });
assert.equal(prevented, true);
assert.equal(documentMock.activeElement, manage.controls[0], "Tab must wrap inside the modal");

documentMock.activeElement = manage.controls[1];
vm.runInContext('openDialog("editor")', context);
assert.equal(editor.inert, false);
assert.equal(manage.inert, true, "only the nested top dialog may remain interactive");
assert.equal(documentMock.activeElement, editor.controls[0]);

vm.runInContext('closeDialog("editor")', context);
assert.equal(editor.inert, true);
assert.equal(manage.inert, false);
assert.equal(documentMock.activeElement, manage.controls[1], "nested close must restore its opener");

vm.runInContext('closeDialog("manage")', context);
assert.equal(header.inert, false);
assert.equal(header.getAttribute("aria-hidden"), null);
assert.equal(documentMock.activeElement, external, "final close must restore dashboard focus");

console.log("Accessibility dialog tests passed.");
