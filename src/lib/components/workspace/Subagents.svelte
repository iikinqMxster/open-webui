<script lang="ts">
	import { toast } from 'svelte-sonner';
	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { onMount, getContext, tick, onDestroy } from 'svelte';
	const i18n = getContext('i18n');

	import { WEBUI_NAME, user, subagents as _subagents, workspaceActions } from '$lib/stores';
	import { goto } from '$app/navigation';
	import {
		getSubagents,
		getSubagentById,
		getSubagentItems,
		deleteSubagentById,
		toggleSubagentById
	} from '$lib/apis/subagents';
	import { capitalizeFirstLetter } from '$lib/utils';

	import Tooltip from '../common/Tooltip.svelte';
	import DeleteConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import EllipsisHorizontal from '../icons/EllipsisHorizontal.svelte';
	import GarbageBin from '../icons/GarbageBin.svelte';
	import Search from '../icons/Search.svelte';
	import XMark from '../icons/XMark.svelte';
	import Spinner from '../common/Spinner.svelte';
	import ViewSelector from './common/ViewSelector.svelte';
	import Badge from '$lib/components/common/Badge.svelte';
	import Switch from '../common/Switch.svelte';
	import SubagentMenu from './Subagents/SubagentMenu.svelte';
	import Pagination from '../common/Pagination.svelte';

	let shiftKey = false;
	let loaded = false;

	let query = '';
	let searchDebounceTimer: ReturnType<typeof setTimeout>;

	let selectedSubagent = null;
	let showDeleteConfirm = false;

	let filteredItems = null;
	let total = null;
	let loading = false;

	let tagsContainerElement: HTMLDivElement;
	let viewOption = '';
	let page = 1;

	$: if (loaded) {
		workspaceActions.set([
			{
				id: 'subagents-new',
				label: $i18n.t('Create'),
				href: '/workspace/subagents/create',
				visible: $user?.role === 'admin' || $user?.permissions?.workspace?.subagents
			}
		]);
	}

	const loadSubagentItems = async () => {
		if (!loaded) return;

		loading = true;
		try {
			const res = await getSubagentItems(localStorage.token, query, viewOption, page).catch(
				(error) => {
					toast.error(`${error}`);
					return null;
				}
			);

			if (res) {
				filteredItems = res.items;
				total = res.total;
			}
		} catch (err) {
			console.error(err);
		} finally {
			loading = false;
		}
	};

	const handleSearchInput = () => {
		loading = true;
		clearTimeout(searchDebounceTimer);
		searchDebounceTimer = setTimeout(() => {
			if (page !== 1) {
				page = 1;
			} else {
				loadSubagentItems();
			}
		}, 300);
	};

	// Immediate response to page/filter changes
	$: if (loaded && page && viewOption !== undefined) {
		loadSubagentItems();
	}

	const cloneHandler = async (subagent) => {
		const _subagent = await getSubagentById(localStorage.token, subagent.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (_subagent) {
			sessionStorage.subagent = JSON.stringify({
				..._subagent,
				id: '',
				handle: `${_subagent.handle ?? ''}-clone`,
				name: `${_subagent.name} (Clone)`
			});
			goto('/workspace/subagents/create');
		}
	};

	const exportHandler = async (subagent) => {
		const _subagent = await getSubagentById(localStorage.token, subagent.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (_subagent) {
			let blob = new Blob([JSON.stringify([_subagent])], {
				type: 'application/json'
			});
			saveAs(blob, `subagent-${_subagent.id}-export-${Date.now()}.json`);
		}
	};

	const deleteHandler = async (subagent) => {
		const res = await deleteSubagentById(localStorage.token, subagent.id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			toast.success($i18n.t('Subagent deleted successfully'));
		}

		page = 1;
		loadSubagentItems();
		await _subagents.set(await getSubagents(localStorage.token));
	};

	onMount(async () => {
		viewOption = localStorage?.workspaceViewOption || '';
		loaded = true;

		const onKeyDown = (event) => {
			if (event.key === 'Shift') {
				shiftKey = true;
			}
		};

		const onKeyUp = (event) => {
			if (event.key === 'Shift') {
				shiftKey = false;
			}
		};

		const onBlur = () => {
			shiftKey = false;
		};

		window.addEventListener('keydown', onKeyDown);
		window.addEventListener('keyup', onKeyUp);
		window.addEventListener('blur', onBlur);

		return () => {
			clearTimeout(searchDebounceTimer);
			window.removeEventListener('keydown', onKeyDown);
			window.removeEventListener('keyup', onKeyUp);
			window.removeEventListener('blur', onBlur);
		};
	});

	onDestroy(() => {
		clearTimeout(searchDebounceTimer);
	});
</script>

<svelte:head>
	<title>
		{$i18n.t('Subagents')} • {$WEBUI_NAME}
	</title>
</svelte:head>

{#if loaded}
	<div class="space-y-1">
		<div class="flex h-8 w-full items-center gap-2">
			<div class="flex min-w-0 flex-1">
				<div class=" self-center ml-1 mr-3">
					<Search className="size-3.5" />
				</div>
				<input
					class=" w-full text-sm pr-4 py-1 rounded-r-xl outline-hidden bg-transparent"
					bind:value={query}
					on:input={handleSearchInput}
					aria-label={$i18n.t('Search Subagents')}
					placeholder={$i18n.t('Search Subagents')}
				/>
				{#if query}
					<div class="self-center pl-1.5 translate-y-[0.5px] rounded-l-xl bg-transparent">
						<button
							class="p-0.5 rounded-full hover:bg-gray-100 dark:hover:bg-gray-900 transition"
							aria-label={$i18n.t('Clear search')}
							on:click={() => {
								query = '';
								handleSearchInput();
							}}
						>
							<XMark className="size-3" strokeWidth="2" />
						</button>
					</div>
				{/if}
			</div>

			<div
				class="flex max-w-[55%] shrink-0 overflow-x-auto scrollbar-none"
				bind:this={tagsContainerElement}
				on:wheel={(e) => {
					if (e.deltaY !== 0) {
						e.preventDefault();
						e.currentTarget.scrollLeft += e.deltaY;
					}
				}}
			>
				<div
					class="flex w-fit gap-0.5 text-center text-sm rounded-full bg-transparent whitespace-nowrap"
				>
					<ViewSelector
						bind:value={viewOption}
						align="end"
						onChange={async (value) => {
							localStorage.workspaceViewOption = value;
							page = 1;
							await tick();
						}}
					/>
				</div>
			</div>
		</div>

		{#if filteredItems === null || loading}
			<div class="w-full h-full flex justify-center items-center my-16 mb-24">
				<Spinner className="size-5" />
			</div>
		{:else if (filteredItems ?? []).length !== 0}
			<div class="my-1 gap-x-2 gap-y-0.5 grid lg:grid-cols-2">
				{#each filteredItems as subagent}
					<Tooltip content={subagent?.description ?? subagent?.handle ?? subagent?.name}>
						<div
							class="flex space-x-4 text-left w-full px-2.5 py-1.5 transition rounded-2xl {subagent.write_access
								? 'cursor-pointer hover:bg-gray-50/70 dark:hover:bg-gray-850/50'
								: 'cursor-not-allowed opacity-60'}"
						>
							{#if subagent.write_access}
								<a
									class=" flex flex-1 space-x-3.5 cursor-pointer w-full"
									href={`/workspace/subagents/edit?id=${encodeURIComponent(subagent.id)}`}
								>
									<div class="flex items-center text-left">
										<div class=" flex-1 self-center">
											<Tooltip content={subagent.handle ?? subagent.name} placement="top-start">
												<div class="flex items-center gap-2">
													<div class="line-clamp-1 text-sm">
														{subagent.name}
													</div>
													{#if subagent.handle}
														<div class="text-xs text-gray-500 shrink-0 truncate">
															{subagent.handle}
														</div>
													{/if}
													{#if subagent?.meta?.remote?.enabled}
														<Badge type="muted" content={$i18n.t('Remote')} />
													{/if}
													{#if !subagent.is_active}
														<Badge type="muted" content={$i18n.t('Inactive')} />
													{/if}
												</div>
											</Tooltip>
											<div class="px-0.5">
												<div class="text-xs text-gray-500 shrink-0">
													<Tooltip
														content={subagent?.user?.email ?? $i18n.t('Deleted User')}
														className="flex shrink-0"
														placement="top-start"
													>
														{$i18n.t('By {{name}}', {
															name: capitalizeFirstLetter(
																subagent?.user?.name ?? subagent?.user?.email ?? $i18n.t('Deleted User')
															)
														})}
													</Tooltip>
												</div>
											</div>
										</div>
									</div>
								</a>
							{:else}
								<div class=" flex flex-1 space-x-3.5 w-full">
									<div class="flex items-center text-left w-full">
										<div class="flex-1 self-center w-full">
											<div class="flex items-center justify-between w-full gap-2">
												<Tooltip content={subagent.handle ?? subagent.name} placement="top-start">
													<div class="flex items-center gap-2">
														<div class="line-clamp-1 text-sm">
															{subagent.name}
														</div>
														{#if subagent.handle}
															<div class="text-xs text-gray-500 shrink-0 truncate">
																{subagent.handle}
															</div>
														{/if}
														{#if subagent?.meta?.remote?.enabled}
															<Badge type="muted" content={$i18n.t('Remote')} />
														{/if}
														{#if !subagent.is_active}
															<Badge type="muted" content={$i18n.t('Inactive')} />
														{/if}
													</div>
												</Tooltip>
												<Badge type="muted" content={$i18n.t('Read Only')} />
											</div>
											<div class="px-0.5">
												<div class="text-xs text-gray-500 shrink-0">
													<Tooltip
														content={subagent?.user?.email ?? $i18n.t('Deleted User')}
														className="flex shrink-0"
														placement="top-start"
													>
														{$i18n.t('By {{name}}', {
															name: capitalizeFirstLetter(
																subagent?.user?.name ?? subagent?.user?.email ?? $i18n.t('Deleted User')
															)
														})}
													</Tooltip>
												</div>
											</div>
										</div>
									</div>
								</div>
							{/if}
							{#if subagent.write_access}
								<div class="flex flex-row gap-0.5 self-center">
									{#if shiftKey}
										<Tooltip content={$i18n.t('Delete')}>
											<button
												class="self-center w-fit text-sm px-2 py-2 dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5 rounded-xl"
												type="button"
												aria-label={$i18n.t('Delete')}
												on:click={() => {
													deleteHandler(subagent);
												}}
											>
												<GarbageBin />
											</button>
										</Tooltip>
									{:else}
										<SubagentMenu
											editHandler={() => {
												goto(`/workspace/subagents/edit?id=${encodeURIComponent(subagent.id)}`);
											}}
											cloneHandler={() => {
												cloneHandler(subagent);
											}}
											exportHandler={() => {
												exportHandler(subagent);
											}}
											deleteHandler={async () => {
												selectedSubagent = subagent;
												showDeleteConfirm = true;
											}}
											onClose={() => {}}
										>
											<button
												class="self-center w-fit text-sm p-1.5 dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5 rounded-xl"
												type="button"
											>
												<EllipsisHorizontal className="size-5" />
											</button>
										</SubagentMenu>
									{/if}

									<button on:click|stopPropagation|preventDefault>
										<Tooltip
											content={subagent.is_active ? $i18n.t('Enabled') : $i18n.t('Disabled')}
										>
											<Switch
												bind:state={subagent.is_active}
												on:change={async () => {
													toggleSubagentById(localStorage.token, subagent.id);
												}}
											/>
										</Tooltip>
									</button>
								</div>
							{/if}
						</div>
					</Tooltip>
				{/each}
			</div>

			{#if total > 30}
				<div class="flex justify-center mt-4 mb-2">
					<Pagination bind:page count={total} perPage={30} />
				</div>
			{/if}
		{:else}
			<div class=" w-full h-full flex flex-col justify-center items-center my-16 mb-24">
				<div class="max-w-md text-center">
					<div class=" text-3xl mb-3">🤖</div>
					<div class=" text-lg font-normal mb-1">{$i18n.t('No subagents found')}</div>
					<div class=" text-gray-500 text-center text-xs">
						{$i18n.t('Try adjusting your search or filter to find what you are looking for.')}
					</div>
				</div>
			</div>
		{/if}
	</div>

	<DeleteConfirmDialog
		bind:show={showDeleteConfirm}
		title={$i18n.t('Delete subagent?')}
		on:confirm={() => {
			deleteHandler(selectedSubagent);
		}}
	>
		<div class=" text-sm text-gray-500 truncate">
			{$i18n.t('This will delete')} <span class="  font-normal">{selectedSubagent.name}</span>.
		</div>
	</DeleteConfirmDialog>
{:else}
	<div class="w-full h-full flex justify-center items-center">
		<Spinner className="size-5" />
	</div>
{/if}
