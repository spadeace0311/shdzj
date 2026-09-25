import { useState, type FormEvent } from "react";

import { login } from "../api/client";

interface LoginPageProps {
  onLoginSuccess?: () => void;
}

export function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError("请输入用户名和密码");
      return;
    }

    setError("");
    setLoading(true);
    try {
      await login(username.trim(), password);
      onLoginSuccess?.();
    } catch (caught) {
      setError(
        typeof caught === "object" &&
          caught !== null &&
          "status" in caught &&
          caught.status === 401
          ? "用户名或密码错误"
          : "登录失败，请稍后重试",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-screen">
      <section className="login-frame" aria-labelledby="login-title">
        <div className="login-brand">
          <p className="eyebrow">上海市地震应急辅助决策系统</p>
          <h1 id="login-title">事件控制台</h1>
          <p>使用本地账号登录，进入事件接入与响应研判工作台。</p>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <label htmlFor="username">用户名</label>
          <input
            id="username"
            name="username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />

          <label htmlFor="password">密码</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />

          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : null}

          <button className="primary-button" type="submit" disabled={loading}>
            {loading ? "登录中" : "登录"}
          </button>
        </form>
      </section>
    </main>
  );
}
