import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { login } from "../src/api/client";
import { LoginPage } from "../src/pages/LoginPage";

vi.mock("../src/api/client", () => ({
  login: vi.fn(),
}));

const loginMock = vi.mocked(login);

test("shows a clear invalid credentials state", async () => {
  loginMock.mockRejectedValue(Object.assign(new Error("invalid"), { status: 401 }));

  render(<LoginPage onLoginSuccess={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "operator" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "wrong-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));

  expect(await screen.findByText("用户名或密码错误")).toBeInTheDocument();
});
