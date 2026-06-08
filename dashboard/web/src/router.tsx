import { createBrowserRouter } from "react-router-dom";
import { Layout } from "./components/layout/Layout";
import Compare from "./pages/Compare";
import StrategyDetail from "./pages/StrategyDetail";
import FactorReport from "./pages/FactorReport";
import Testnet from "./pages/Testnet";
import PipelineStatus from "./pages/PipelineStatus";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Compare /> },
      { path: "strategies/:id", element: <StrategyDetail /> },
      { path: "factors", element: <FactorReport /> },
      { path: "testnet", element: <Testnet /> },
      { path: "pipeline", element: <PipelineStatus /> },
    ],
  },
]);
