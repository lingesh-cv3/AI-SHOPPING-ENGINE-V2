import { useEffect } from "react";
import { getConnection } from "./api";
import { themeFor, type MerchantTheme } from "./theme";

/** Apply a merchant's theme to the document root.
 *
 *  Every token in the stylesheet keys off data-theme on the root element, so this
 *  one attribute changes the whole page and no component needs to know which
 *  merchant it is rendering.
 *
 *  Extracted because App was the only thing setting it, and the new pages do not go
 *  through App - so they rendered with every variable unresolved: no card, no
 *  border, and a password field the same colour as the page behind it. Copying the
 *  effect into three files would have worked and would have drifted.
 */
export function useTheme(): MerchantTheme {
  const merchant = themeFor(getConnection());

  useEffect(() => {
    document.documentElement.dataset.theme = merchant.theme;
    document.title = merchant.name;
  }, [merchant]);

  return merchant;
}