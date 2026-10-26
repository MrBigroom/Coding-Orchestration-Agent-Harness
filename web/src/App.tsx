import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Adapters from "./pages/Adapters";
import Agents from "./pages/Agents";
import BuildLog from "./pages/BuildLog";
import Dashboard from "./pages/Dashboard";
import Memory from "./pages/Memory";
import Models from "./pages/Models";
import Projects from "./pages/Projects";
import PromptPreview from "./pages/PromptPreview";
import Schedules from "./pages/Schedules";
import Settings from "./pages/Settings";
import TaskDetail from "./pages/TaskDetail";
import Tasks from "./pages/Tasks";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/tasks/:id" element={<TaskDetail />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/models" element={<Models />} />
        <Route path="/adapters" element={<Adapters />} />
        <Route path="/memory" element={<Memory />} />
        <Route path="/prompt-preview" element={<PromptPreview />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/build-log" element={<BuildLog />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<div className="text-muted">Not found.</div>} />
      </Routes>
    </Layout>
  );
}
