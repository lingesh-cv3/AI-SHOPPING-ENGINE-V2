import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import { MerchantPage } from "./pages/MerchantPage.tsx";
import { OperationsPage } from "./pages/OperationsPage.tsx";
import { SignInPage } from "./pages/SignInPage.tsx";
import "./styles.css";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
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
        {/* One address per merchant, because the address is now what decides
            which shop this is. A shopper can bookmark /kettle or send it to
            somebody and land in the same place.

            A real deployment has one merchant per domain and no chooser at all.
            Two paths here because the demo carries two clients, and showing that
            the same engine serves both is the point of it. */}
        <Route path="/northfield" element={<App />} />
        <Route path="/kettle" element={<App />} />

        <Route path="/signin" element={<SignInPage />} />
        <Route path="/signup" element={<SignInPage creating />} />
        <Route path="/merchant" element={<MerchantPage />} />
        <Route path="/operations" element={<OperationsPage />} />

        {/* A bare address lands in a shop rather than on a chooser. Asking a
            shopper which of our clients they meant is a question only we care
            about. */}
        <Route path="/" element={<Navigate to="/northfield" replace />} />
        <Route path="*" element={<Navigate to="/northfield" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);