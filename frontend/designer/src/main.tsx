import { createRoot } from "react-dom/client";
import { DesignerRoot } from "./DesignerRoot";

// Entry for the designer island bundle (dist/designer.js). No-ops on every
// page that doesn't render the mount point — which, in this PR, is every
// page: designer.html still renders the Alpine UI and doesn't load this
// script yet. PR B swaps designer.html's markup for
// `<div id="meso-designer-root">` and the module <script> tag; this file is
// what mounts into it.
const container = document.getElementById("meso-designer-root");

if (container) {
  createRoot(container).render(<DesignerRoot />);
}
