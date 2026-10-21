import { useState, useRef, useEffect, useCallback } from 'react';
import api from '../api';

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 600000;

export const useSubmission = () => {
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const pollTimerRef = useRef(null);
  const runTokenRef = useRef(0);

  useEffect(() => {
    return () => {
      clearTimeout(pollTimerRef.current);
      runTokenRef.current += 1;
    };
  }, []);

  const handleSubmit = useCallback(async ({ problemId, language, code, contestId, user }) => {
    if (!user) {
      alert('Please login to submit code');
      return;
    }

    clearTimeout(pollTimerRef.current);
    const token = runTokenRef.current + 1;
    runTokenRef.current = token;
    setSubmitting(true);
    setResult(null);

    const payload = {
      problem_id: Number(problemId),
      language: language === 'python' ? 'py' : language,
      src_code: code,
    };
    if (contestId) payload.contest_id = Number(contestId);

    let submissionId;
    try {
      const response = await api.post('/submit', payload);
      if (runTokenRef.current !== token) return;
      submissionId = response.data.submission_id;
      setResult({ status: 'JUDGING' });
    } catch (err) {
      if (runTokenRef.current !== token) return;
      console.error('Submission failed', err);
      setResult({
        status: 'Error',
        message: err.response?.data?.detail || 'Failed to submit code',
      });
      setSubmitting(false);
      return;
    }

    const deadline = Date.now() + POLL_TIMEOUT_MS;
    const poll = async () => {
      if (runTokenRef.current !== token) return;
      try {
        const res = await api.get(`/submissions/${submissionId}`);
        if (runTokenRef.current !== token) return;
        const data = res.data;
        if (data.status === 'PENDING') {
          if (Date.now() >= deadline) {
            setResult({ status: 'Error', message: 'Judging is taking too long; try again later.' });
            setSubmitting(false);
            return;
          }
          pollTimerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
          return;
        }

        setResult({
          status: data.status,
          time: data.execution_time_ms ? `${Number(data.execution_time_ms).toFixed(1)}ms` : '-',
          memory: data.peak_memory_mb ? `${Number(data.peak_memory_mb).toFixed(1)}MB` : '-',
          message: data.message || (data.status !== 'AC' ? `Verdict: ${data.status}` : ''),
        });
        setSubmitting(false);
      } catch (err) {
        if (runTokenRef.current !== token) return;
        console.error('Polling failed', err);
        setResult({
          status: 'Error',
          message: err.response?.data?.detail || 'Failed to check execution status',
        });
        setSubmitting(false);
      }
    };

    poll();
  }, []);

  return {
    submitting,
    result,
    setResult,
    handleSubmit,
  };
};

export default useSubmission;
