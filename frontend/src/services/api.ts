import axios from "axios";

const API = axios.create({
  baseURL: "/api/v1/skillpack",
  headers: { "Content-Type": "application/json" },
});

export const getState = () => API.get("/state");
export const getStatus = () => API.get("/status");
export const getSettings = () => API.get("/settings");
export const getSignal = () => API.get("/signal");
export const getScan = () => API.get("/scan");

export const startTrader = () => API.post("/start");
export const stopTrader = () => API.post("/stop");
export const updateSettings = (data: any) => API.post("/settings", data);
export const runSkill = (data: any) => API.post("/run-skill", data);
export const closePosition = (id: string) => API.post(`/positions/${id}/close`);

export type { Position, Order, Signal, Settings } from "../store/useTraderStore";
