import { createBrowserRouter, Navigate } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";
import { DailyReview } from "@/pages/DailyReview";
import { Intel } from "@/pages/Intel";
import { SectorHub } from "@/pages/SectorHub";
import { SectorDetail } from "@/pages/SectorDetail";
import { Portfolio } from "@/pages/Portfolio";
import { StockData } from "@/pages/StockData";
import { Liquidity } from "@/pages/Liquidity";
import { Macro } from "@/pages/Macro";
import { Gold } from "@/pages/Gold";
import { Watchlist } from "@/pages/Watchlist";
import { Funds } from "@/pages/Funds";
import { ResearchHub } from "@/pages/ResearchHub";
import { Settings } from "@/pages/Settings";
import { Login } from "@/pages/Login";
import { RequireAuth } from "@/components/common/RequireAuth";

export const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { path: "/", element: <Navigate to="/daily-review" replace /> },
      { path: "/daily-review", element: <DailyReview /> },
      { path: "/intel", element: <Intel /> },
      { path: "/sectors", element: <SectorHub /> },
      { path: "/sectors/:key", element: <SectorDetail /> },
      { path: "/sector-scores", element: <Navigate to="/sectors" replace /> },
      { path: "/portfolio", element: <Portfolio /> },
      { path: "/funds", element: <Funds /> },
      { path: "/stock-data", element: <StockData /> },
      { path: "/macro", element: <Macro /> },
      { path: "/gold", element: <Gold /> },
      { path: "/liquidity", element: <Liquidity /> },
      { path: "/watchlist", element: <Watchlist /> },
      { path: "/research", element: <ResearchHub /> },
      { path: "/my-reports", element: <Navigate to="/research" replace /> },
      { path: "/notes", element: <Navigate to="/research" replace /> },
      { path: "/settings", element: <Settings /> },
    ],
  },
]);
