import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App.tsx";
import { MerchantPage } from "./pages/MerchantPage.tsx";
import { OperationsPage } from "./pages/OperationsPage.tsx";
import { SignInPage } from "./pages/SignInPage.tsx";
import "./styles.css";

/**
 * Real URLs, one per audience.
 *
 * The three consoles were tabs in one page, which meant a merchant and a CV3
 * operator arrived at the same address and the interface decided who they were.
 * Separate routes put that decision where it belongs: each page asks for its own
 * credential, and holding one does not get you the others.
 *
 * The shop keeps its own internal navigation for products and orders. Turning
 * those into URLs is worth doing - a shopper should be able to send somebody a
 * link to a shoe - but it means rewriting a file that currently works, and that is
 * its own task rather than something to fold into this one.
 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/signin" element={<SignInPage />} />
        <Route path="/signup" element={<SignInPage creating />} />
        <Route path="/merchant" element={<MerchantPage />} />
        <Route path="/operations" element={<OperationsPage />} />

        {/* Anything else is the shop. A wrong URL should land somebody
            somewhere useful rather than on an apology. */}
        <Route path="*" element={<App />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);