import { useState, useRef, useEffect, useCallback } from 'react';
import api from '../api';

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 900000;

const formatMs = (value) =>
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(1)}ms`;

const formatMb = (value) =>
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(1)}MB`;

export const useRunBatch = () => {
  const [activeTab, setActiveTab] = useState('sample-0');
  const [customTests, setCustomTests] = useState([]);
  const [runResults, setRunResults] = useState({});
  const [runningTests, setRunningTests] = useState(false);

  const pollTimerRef = useRef(null);
  const runTokenRef = useRef(0);

  useEffect(() => {
    return () => {
      clearTimeout(pollTimerRef.current);
      runTokenRef.current += 1;
    };
  }, []);

  const addCustomTest = useCallback(() => {
    if (customTests.length < 4) {
      const newId = `custom-${customTests.length}`;
      setCustomTests((prev) => [...prev, { input: '', expectedOutput: '' }]);
      setActiveTab(newId);
    }
  }, [customTests.length]);

  const updateCustomTest = useCallback((index, field, value) => {
    setCustomTests((prev) => {
      const copy = [...prev];
      if (copy[index]) {
        copy[index] = { ...copy[index], [field]: value };
      }
      return copy;
    });
  }, []);

  const deleteCustomTest = useCallback((index) => {
    const tabId = `custom-${index}`;
    setCustomTests((prev) => prev.filter((_, idx) => idx !== index));
    setActiveTab((curr) => (curr === tabId ? 'sample-0' : curr));
  }, []);

  const handleRunTests = useCallback(
    async ({ problem, language, code, user }) => {
      if (!user) {
        alert('Please login to run code.');
        return;
      }
      if (!problem) return;

      clearTimeout(pollTimerRef.current);
      const token = runTokenRef.current + 1;
      runTokenRef.current = token;

      setRunningTests(true);
      setRunResults({});

      const samplesToRun = (problem.samples || []).map((sample, index) => ({
        tabId: `sample-${index}`,
        input: sample.input,
        expected: sample.output,
      }));

      const customToRun = customTests.map((custom, index) => ({
        tabId: `custom-${index}`,
        input: custom.input,
        expected: custom.expectedOutput,
      }));

      const allTests = [...samplesToRun, ...customToRun];
      if (allTests.length === 0) {
        setRunningTests(false);
        return;
      }

      const initialResults = {};
      allTests.forEach((test) => {
        initialResults[test.tabId] = { status: 'RUNNING' };
      });
      setRunResults(initialResults);

      const markAllError = (message = 'Run failed') => {
        if (runTokenRef.current !== token) return;
        const errorResults = {};
        allTests.forEach((test) => {
          errorResults[test.tabId] = { status: 'Error', message };
        });
        setRunResults(errorResults);
        setRunningTests(false);
      };

      let batchId;
      try {
        const response = await api.post('/run_batch', {
          language: language === 'python' ? 'py' : language,
          time_limit: problem.timeLimit || 2000,
          memory_limit: problem.memoryLimit || 256,
          src_code: code,
          tests: allTests.map((test) => ({
            input: test.input || ' ',
            expected_output: test.expected || null,
          })),
        });
        batchId = response.data.batch_id;
      } catch (err) {
        console.error('Batch run failed', err);
        markAllError(err.response?.data?.detail || 'Failed to queue run');
        return;
      }

      const deadline = Date.now() + POLL_TIMEOUT_MS;
      const poll = async () => {
        if (runTokenRef.current !== token) return;
        try {
          const res = await api.get(`/runs/${batchId}`);
          const data = res.data;
          if (data.status === 'PENDING') {
            if (Date.now() >= deadline) {
              markAllError('Run is taking too long; try again later.');
              return;
            }
            pollTimerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
            return;
          }

          if (data.status === 'SYSTEM_ERROR' || !Array.isArray(data.tests)) {
            markAllError(data.message || 'Judge failed to run this batch');
            return;
          }

          const updatedResults = {};
          allTests.forEach((test, index) => {
            const result = data.tests[index] || {
              status: 'Error',
              std_out: '',
              message: 'No result returned for this test',
            };
            updatedResults[test.tabId] = {
              status: result.status || 'Error',
              output: result.std_out ?? '',
              message: result.message || '',
              expectedOutput: test.expected,
              time: formatMs(result.execution_time_ms),
              memory: formatMb(result.peak_memory_mb),
            };
          });
          setRunResults(updatedResults);
          setRunningTests(false);
        } catch (err) {
          console.error('Batch polling failed', err);
          markAllError(err.response?.data?.detail || 'Failed to check run status');
        }
      };

      poll();
    },
    [customTests]
  );

  return {
    activeTab,
    setActiveTab,
    customTests,
    setCustomTests,
    runResults,
    runningTests,
    addCustomTest,
    updateCustomTest,
    deleteCustomTest,
    handleRunTests,
  };
};

export default useRunBatch;
