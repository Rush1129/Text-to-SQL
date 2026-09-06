import { useState, useEffect } from 'react';
import LoginPage from './pages/LoginPage';
import ChatPage from './pages/ChatPage';
import DetailsPage from './pages/DetailsPage';
import AuditPage from './pages/AuditPage';
import Sidebar from './components/Sidebar';
import useStore from './store';

function getRoute() {
  const hash = window.location.hash.replace('#/', '');
  return hash || '';
}

export default function App() {
  const loggedIn = useStore((s) => s.loggedIn);
  const [route, setRoute] = useState(getRoute());

  useEffect(() => {
    const onHashChange = () => setRoute(getRoute());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  if (!loggedIn) {
    return <LoginPage />;
  }

  let page;
  switch (route) {
    case 'details':
      page = <DetailsPage />;
      break;
    case 'audit':
      page = <AuditPage />;
      break;
    default:
      page = <ChatPage />;
  }

  return (
    <div className="app-layout">
      <Sidebar />
      {page}
    </div>
  );
}
