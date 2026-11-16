import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import SubmitPage from "./pages/SubmitPage";
import AdminPage from "./pages/AdminPage";
import WelcomePage from "./pages/WelcomePage";
import InspirationPage from "./pages/InspirationPage";
import { readIdentity } from "./lib/identity";

function RequireIdentity({ children }: { children: React.ReactNode }) {
  return readIdentity() ? children : <Navigate to="/welcome" replace />;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/welcome" replace />} />
        <Route path="/welcome" element={<WelcomePage />} />
        <Route path="/inspiration" element={<RequireIdentity><InspirationPage /></RequireIdentity>} />
        <Route path="/submit" element={<RequireIdentity><SubmitPage /></RequireIdentity>} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="*" element={<Navigate to="/welcome" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
