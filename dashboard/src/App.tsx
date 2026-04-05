import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/tooltip';
import { AppLayout } from '@/components/layout/AppLayout';
import { ErrorBoundary } from '@/components/shared/ErrorBoundary';
import { DashboardPage } from '@/pages/DashboardPage';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { ProjectDetailPage } from '@/pages/ProjectDetailPage';
import { TasksPage } from '@/pages/TasksPage';
import { EventsPage } from '@/pages/EventsPage';
import { MeetingsPage } from '@/pages/MeetingsPage';
import { MeetingDetailPage } from '@/pages/MeetingDetailPage';
import { SettingsPage } from '@/pages/SettingsPage';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import { ReportsPage } from '@/pages/ReportsPage';
import { AgentsPage } from '@/pages/AgentsPage';
import { AgentLivePage } from '@/pages/AgentLivePage';
import { BriefingsPage } from '@/pages/BriefingsPage';
import { PipelinesPage } from '@/pages/PipelinesPage';
import { FailuresPage } from '@/pages/FailuresPage';
import { PromptsPage } from '@/pages/PromptsPage';
import { useLanguage, LanguageContext } from '@/i18n';
import { ProjectProvider } from '@/context/ProjectContext';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
});

function AppWithLanguage() {
  const language = useLanguage();
  return (
    <LanguageContext.Provider value={language}>
      <ProjectProvider>
      <BrowserRouter>
          <Routes>
            <Route element={<AppLayout />}>
              <Route index element={<ErrorBoundary><DashboardPage /></ErrorBoundary>} />
              <Route path="projects" element={<ErrorBoundary><ProjectsPage /></ErrorBoundary>} />
              <Route path="projects/:projectId" element={<ErrorBoundary><ProjectDetailPage /></ErrorBoundary>} />
              <Route path="tasks" element={<ErrorBoundary><TasksPage /></ErrorBoundary>} />
              <Route path="events" element={<ErrorBoundary><EventsPage /></ErrorBoundary>} />
              <Route path="meetings" element={<ErrorBoundary><MeetingsPage /></ErrorBoundary>} />
              <Route path="meetings/:meetingId" element={<ErrorBoundary><MeetingDetailPage /></ErrorBoundary>} />
              <Route path="analytics" element={<ErrorBoundary><AnalyticsPage /></ErrorBoundary>} />
              <Route path="agents" element={<ErrorBoundary><AgentsPage /></ErrorBoundary>} />
              <Route path="agent-live" element={<ErrorBoundary><AgentLivePage /></ErrorBoundary>} />
              <Route path="briefings" element={<ErrorBoundary><BriefingsPage /></ErrorBoundary>} />
              <Route path="reports" element={<ErrorBoundary><ReportsPage /></ErrorBoundary>} />
              <Route path="pipelines" element={<ErrorBoundary><PipelinesPage /></ErrorBoundary>} />
              <Route path="failures" element={<ErrorBoundary><FailuresPage /></ErrorBoundary>} />
              <Route path="prompts" element={<ErrorBoundary><PromptsPage /></ErrorBoundary>} />
              <Route path="settings" element={<ErrorBoundary><SettingsPage /></ErrorBoundary>} />
            </Route>
          </Routes>
        </BrowserRouter>
      </ProjectProvider>
    </LanguageContext.Provider>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <AppWithLanguage />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
