import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_use.agent.views import AgentOutput, ActionResult
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import DOMInteractedElement

logger = logging.getLogger(__name__)


@dataclass
class CachedAction:
	"""A single cached action from a browser-use agent step."""
	step_number: int
	action_type: str
	action_params: dict[str, Any]
	element: dict[str, Any] | None
	result_success: bool
	result_error: str | None
	result_content: str | None
	url: str
	timestamp: float

	def to_dict(self) -> dict[str, Any]:
		return {
			'step_number': self.step_number,
			'action_type': self.action_type,
			'action_params': self.action_params,
			'element': self.element,
			'result_success': self.result_success,
			'result_error': self.result_error,
			'result_content': self.result_content,
			'url': self.url,
			'timestamp': self.timestamp,
		}


@dataclass
class ActionCache:
	"""Collects actions taken by the agent for conversion to deterministic automation."""
	task_description: str
	start_url: str
	actions: list[CachedAction] = field(default_factory=list)
	start_time: float = field(default_factory=time.time)

	def record_step(
		self,
		step_number: int,
		model_output: AgentOutput | None,
		results: list[ActionResult] | None,
		browser_state: BrowserStateSummary | None,
	) -> None:
		if not model_output or not results:
			return

		url = browser_state.url if browser_state else ''
		selector_map = {}
		if browser_state and browser_state.dom_state:
			selector_map = browser_state.dom_state.selector_map

		for action, result in zip(model_output.action, results):
			action_data = action.model_dump(exclude_unset=True)
			action_type = next(iter(action_data.keys())) if action_data else 'unknown'
			action_params = action_data.get(action_type, {})
			if not isinstance(action_params, dict):
				action_params = {'value': action_params}

			element_info = None
			index = action.get_index()
			if index is not None and index in selector_map:
				node = selector_map[index]
				element_info = self._extract_element_info(node)

			cached = CachedAction(
				step_number=step_number,
				action_type=action_type,
				action_params=action_params,
				element=element_info,
				result_success=not bool(result.error),
				result_error=result.error,
				result_content=result.extracted_content,
				url=url,
				timestamp=time.time(),
			)
			self.actions.append(cached)
			logger.info(
				f'[ActionCache] Step {step_number}: {action_type} '
				f'(success={cached.result_success}) '
				f'element={_short_element(element_info)}'
			)

	def _extract_element_info(self, node) -> dict[str, Any]:
		"""Extract stable selector info from an EnhancedDOMTreeNode."""
		info: dict[str, Any] = {
			'tag_name': node.tag_name,
			'xpath': node.xpath,
			'attributes': dict(node.attributes) if node.attributes else {},
		}
		if node.ax_node and node.ax_node.name:
			info['ax_name'] = node.ax_node.name
		if node.node_value:
			info['node_value'] = node.node_value
		text = node.get_all_children_text() if hasattr(node, 'get_all_children_text') else ''
		if text:
			info['text_content'] = text[:200]
		return info

	def get_successful_actions(self) -> list[CachedAction]:
		"""Return only actions that succeeded (no errors)."""
		return [a for a in self.actions if a.result_success]

	def get_deterministic_actions(self) -> list[CachedAction]:
		"""Filter to actions that are deterministic (not redundant exploration).

		Removes:
		- Failed actions (errors)
		- done actions (not replayable)
		- Consecutive duplicate actions on the same element
		"""
		successful = self.get_successful_actions()
		deterministic = []
		seen_actions: set[str] = set()

		for action in successful:
			if action.action_type in ('done',):
				continue

			key = f'{action.action_type}:{json.dumps(action.action_params, sort_keys=True)}'
			if action.element:
				key += f':{action.element.get("xpath", "")}'

			if key in seen_actions:
				logger.debug(f'[ActionCache] Skipping duplicate: {key}')
				continue
			seen_actions.add(key)
			deterministic.append(action)

		return deterministic

	def save(self, path: str | Path) -> None:
		"""Save the full cache to a JSON file."""
		path = Path(path)
		path.parent.mkdir(parents=True, exist_ok=True)
		data = {
			'task_description': self.task_description,
			'start_url': self.start_url,
			'start_time': self.start_time,
			'total_actions': len(self.actions),
			'successful_actions': len(self.get_successful_actions()),
			'deterministic_actions': len(self.get_deterministic_actions()),
			'actions': [a.to_dict() for a in self.actions],
			'deterministic_actions_list': [a.to_dict() for a in self.get_deterministic_actions()],
		}
		with open(path, 'w') as f:
			json.dump(data, f, indent=2)
		logger.info(f'[ActionCache] Saved {len(self.actions)} actions to {path}')


def _short_element(el: dict[str, Any] | None) -> str:
	if not el:
		return 'None'
	tag = el.get('tag_name', '?')
	attrs = el.get('attributes', {})
	name = attrs.get('name', attrs.get('id', attrs.get('type', '')))
	return f'<{tag} {name}>' if name else f'<{tag}>'
