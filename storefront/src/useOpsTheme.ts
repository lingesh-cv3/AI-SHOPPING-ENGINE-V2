import { useEffect } from "react";

/**
 * CV3's own look for the Operations console.
 *
 * Every colour in the stylesheet is a token keyed off `data-theme` on the root. The
 * old Operations page called `useTheme()`, which applies whichever merchant's theme
 * matches the address - and the bare /operations address falls back to the last shop
 * visited - so a cross-merchant CV3 console wore Northfield's or Kettle & Bloom's
 * palette and put that merchant's name in the browser tab. This page belongs to CV3
 * and spans every client; it should look like neither.
 */
export function useOpsTheme() {
  useEffect(() => {
    const root = document.documentElement;
    const previous = root.dataset.theme;
    const previousTitle = document.title;
    root.dataset.theme = "cv3-ops";
    document.title = "CV3 Operations";
    return () => {
      if (previous === undefined) delete root.dataset.theme;
      else root.dataset.theme = previous;
      document.title = previousTitle;
    };
  }, []);
}
