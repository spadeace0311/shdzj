import { useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { clearAccessToken, getCurrentUser } from "./api/client";
import { ArtifactCenterRoute } from "./pages/ArtifactCenterPage";
import { CollectorStatusPage } from "./pages/CollectorStatusPage";
import { DataAssetsPage } from "./pages/DataAssetsPage";
import { EventDetailPage } from "./pages/EventDetailPage";
import { EventListPage } from "./pages/EventListPage";
import { LoginPage } from "./pages/LoginPage";
import { ManualEventPage } from "./pages/ManualEventPage";
import { WorkgroupTasksPage } from "./pages/WorkgroupTasksPage";

interface ConsoleShellProps {
  onLogout: () => void;
  userRole: string;
  userWorkgroup: string | null;
}

function ConsoleShell({
  onLogout,
  userRole,
  userWorkgroup,
}: ConsoleShellProps) {
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
            <NavLink
              to="/collector"
              className={({ isActive }) => (isActive ? "nav-link nav-link--active" : "nav-link")}
            >
              采集状态
            </NavLink>
            <NavLink
              to="/data-assets"
              className={({ isActive }) => (isActive ? "nav-link nav-link--active" : "nav-link")}
            >
              数据资产
            </NavLink>
            <NavLink
              to="/artifacts"
              aria-label="成果中心导航"
              className={({ isActive }) => (isActive ? "nav-link nav-link--active" : "nav-link")}
            >
              成果中心
            </NavLink>
            <NavLink
              to="/tasks"
              className={({ isActive }) =>
                isActive ? "nav-link nav-link--active" : "nav-link"
              }
            >
              工作组任务
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
            <Route path="/collector" element={<CollectorStatusPage />} />
            <Route
              path="/data-assets"
              element={<DataAssetsPage userRole={userRole} />}
            />
            <Route
              path="/artifacts"
              element={
                <ArtifactCenterRoute
                  userRole={userRole}
                  workgroup={userWorkgroup}
                />
              }
            />
            <Route
              path="/artifacts/:eventId"
              element={
                <ArtifactCenterRoute
                  userRole={userRole}
                  workgroup={userWorkgroup}
                />
              }
            />
            <Route
              path="/tasks"
              element={
                <WorkgroupTasksPage
                  userRole={userRole}
                  workgroup={userWorkgroup}
                />
              }
            />
            <Route
              path="/tasks/:eventId"
              element={
                <WorkgroupTasksPage
                  userRole={userRole}
                  workgroup={userWorkgroup}
                />
              }
            />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [userRole, setUserRole] = useState("");
  const [userWorkgroup, setUserWorkgroup] = useState<string | null>(null);

  function handleLogout() {
    clearAccessToken();
    setUserRole("");
    setUserWorkgroup(null);
    setAuthenticated(false);
  }

  async function handleLoginSuccess() {
    try {
      const currentUser = await getCurrentUser();
      setUserRole(currentUser.role);
      setUserWorkgroup(currentUser.workgroup);
      setAuthenticated(true);
    } catch {
      clearAccessToken();
      setUserRole("");
      setUserWorkgroup(null);
      setAuthenticated(false);
    }
  }

  if (!authenticated) {
    return <LoginPage onLoginSuccess={() => void handleLoginSuccess()} />;
  }

  return (
    <BrowserRouter>
      <ConsoleShell
        onLogout={handleLogout}
        userRole={userRole}
        userWorkgroup={userWorkgroup}
      />
    </BrowserRouter>
  );
}
