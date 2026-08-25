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
import { Allocation } from "@/pages/Allocation";
import { Gold } from "@/pages/Gold";
import { Oil } from "@/pages/Oil";
import { Bonds } from "@/pages/Bonds";
import { Factors } from "@/pages/Factors";
import { Pulse } from "@/pages/Pulse";
import { Watchlist } from "@/pages/Watchlist";
import { Screening } from "@/pages/Screening";
import { ResearchHub } from "@/pages/ResearchHub";
import { Settings } from "@/pages/Settings";
import { Login } from "@/pages/Login";
import { DataSourceHealth } from "@/pages/DataSourceHealth";
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
      { path: "/screening", element: <Screening /> },
      { path: "/factors", element: <Factors /> },
      { path: "/funds", element: <Navigate to="/screening" replace /> },
      { path: "/stock-data", element: <StockData /> },
      { path: "/macro", element: <Macro /> },
      { path: "/gold", element: <Gold /> },
      { path: "/oil", element: <Oil /> },
      { path: "/bonds", element: <Bonds /> },
      { path: "/pulse", element: <Pulse /> },
      { path: "/liquidity", element: <Liquidity /> },
      { path: "/allocation", element: <Allocation /> },
      { path: "/watchlist", element: <Watchlist /> },
      { path: "/research", element: <ResearchHub /> },
      { path: "/my-reports", element: <Navigate to="/research" replace /> },
      { path: "/notes", element: <Navigate to="/research" replace /> },
      { path: "/settings", element: <Settings /> },
      { path: "/source-health", element: <DataSourceHealth /> },
    ],
  },
]);
