import { useEffect } from "react";
import { useTraderStore } from "../store/useTraderStore";
import { getSettings, updateSettings } from "../services/api";
import { Navbar, Sidebar } from "../components/layout";
import { SettingsForm } from "../components/SettingsForm";

export const SettingsPage = () => {
  const { settings, updateSettings: storeUpdateSettings } = useTraderStore();

  const load = async () => {
    try {
      const res = await getSettings();
      storeUpdateSettings(res.data);
    } catch (e) {
      // use local defaults
    }
  };

  useEffect(() => {
    load();
  }, [storeUpdateSettings]);

  const handleSave = async () => {
    try {
      await updateSettings(settings);
      alert("Settings saved successfully");
    } catch (e) {
      alert("Failed to save settings");
    }
  };

  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar />
      <div className="flex">
        <Sidebar active="settings" />
        <div className="flex-1 ml-64 pt-16 pb-8">
          <div className="max-w-3xl mx-auto px-6">
            <h1 className="text-2xl font-bold text-slate-900 mb-6">Settings</h1>
            <SettingsForm
              settings={settings}
              onUpdate={(key, value) => {
                storeUpdateSettings({ [key]: value } as any);
              }}
              onSave={handleSave}
            />
          </div>
        </div>
      </div>
    </div>
  );
};
