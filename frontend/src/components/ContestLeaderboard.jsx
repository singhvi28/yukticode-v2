import React, { useState, useEffect } from 'react';
import { Loader, Trophy } from 'lucide-react';
import api from '../api';
import { useAuth } from '../context/AuthContext';

const REFRESH_INTERVAL_MS = 5000;

const ContestLeaderboard = ({ contestId, limit = 100 }) => {
  const { user } = useAuth();
  const [leaderboard, setLeaderboard] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lastUpdated, setLastUpdated] = useState(null);

  useEffect(() => {
    if (!contestId || !user) return undefined;

    let cancelled = false;
    let timer = null;

    const load = async () => {
      try {
        const res = await api.get(`/contests/${contestId}/leaderboard`);
        if (cancelled) return;
        setLeaderboard((res.data?.leaderboard || []).slice(0, limit));
        setLastUpdated(new Date());
        setError('');
      } catch (err) {
        if (cancelled) return;
        setError(err.response?.data?.detail || 'Could not load leaderboard');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    timer = setInterval(load, REFRESH_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [contestId, limit, user]);

  if (!user) {
    return (
      <div className="leaderboard-container glass-card">
        <h3 className="leaderboard-title">
          <Trophy size={18} /> Live Rankings
        </h3>
        <p className="leaderboard-empty">Log in to view the leaderboard.</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="leaderboard-loading">
        <Loader className="spinner" size={24} />
      </div>
    );
  }

  return (
    <div className="leaderboard-container glass-card">
      <h3 className="leaderboard-title">
        <Trophy size={18} /> Live Rankings
      </h3>
      <p className="leaderboard-countdown">
        Refreshes every {REFRESH_INTERVAL_MS / 1000} seconds
        {lastUpdated ? ` · updated ${lastUpdated.toLocaleTimeString()}` : ''}
      </p>
      {error && <p className="leaderboard-reconnecting">{error}</p>}
      <table className="leaderboard-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>User</th>
            <th>Score</th>
            <th>Penalty (Mins)</th>
          </tr>
        </thead>
        <tbody>
          {leaderboard.map((row) => (
            <tr key={row.user_id}>
              <td>{row.rank}</td>
              <td className="font-bold">{row.username}</td>
              <td className="text-success">{row.score}</td>
              <td className="text-warning">{row.penalty}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {leaderboard.length === 0 && (
        <p className="leaderboard-empty">No submissions yet.</p>
      )}
      <style>{`
        .leaderboard-container { padding: 1rem; }
        .leaderboard-title { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; font-size: 1.1rem; }
        .leaderboard-countdown { font-size: 0.8rem; color: var(--text-muted); margin: 0 0 0.75rem 0; }
        .leaderboard-reconnecting { font-size: 0.8rem; color: var(--warning); margin: 0 0 0.5rem 0; }
        .leaderboard-loading { display: flex; justify-content: center; padding: 2rem; }
        .leaderboard-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        .leaderboard-table th, .leaderboard-table td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border-light); }
        .leaderboard-table th { color: var(--text-muted); font-weight: 500; }
        .text-success { color: var(--success); }
        .text-warning { color: var(--warning); }
        .leaderboard-empty { color: var(--text-muted); padding: 1rem; margin: 0; font-size: 0.9rem; }
      `}</style>
    </div>
  );
};

export default ContestLeaderboard;
