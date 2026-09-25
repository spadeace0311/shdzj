import { useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { clearAccessToken, getAccessToken } from "./api/client";
import { EventDetailPage } from "./pages/EventDetailPage";
import { EventListPage } from "./pages/EventListPage";
import { LoginPage } from "./pages/LoginPage";
import { ManualEventPage } from "./pages/ManualEventPage";

interface ConsoleShellProps {
  onLogout: () => void;
}

function ConsoleShell({ onLogout }: ConsoleShellProps) {
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">
            震
          </span>
          <div>
            <p className="eyebrow">上海市地震应急辅助决策系统</p>
            <strong>事件控制台</strong>
          </div>
        </div>
        <div className="header-status">
          <span className="status-dot" aria-hidden="true" />
          <span>系统运行中</span>
          <span className="header-divider" aria-hidden="true" />
          <span>已登录</span>
        </div>
        <button className="ghost-button" type="button" onClick={onLogout}>
          退出登录
        </button>
      </header>

      <div className="app-body">
        <aside className="app-sidebar" aria-label="主导航">
          <nav className="side-nav">
            <NavLink
              to="/"
              end
              className={({ isActive }) => (isActive ? "nav-link nav-link--active" : "nav-link")}
            >
              事件列表
            </NavLink>
            <NavLink
              to="/manual"
              className={({ isActive }) => (isActive ? "nav-link nav-link--active" : "nav-link")}
            >
              人工触发
            </NavLink>
          </nav>
          <div className="sidebar-note">
            <p>当前工作区</p>
            <strong>事件接入</strong>
          </div>
        </aside>

        <main className="app-main">
          <Routes>
            <Route path="/" element={<EventListPage />} />
            <Route path="/events/:eventId" element={<EventDetailPage />} />
            <Route path="/manual" element={<ManualEventPage />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(() => getAccessToken() !== null);

  function handleLogout() {
    clearAccessToken();
    setAuthenticated(false);
  }

  if (!authenticated) {
    return <LoginPage onLoginSuccess={() => setAuthenticated(true)} />;
  }

  return (
    <BrowserRouter>
      <ConsoleShell onLogout={handleLogout} />
    </BrowserRouter>
  );
}
